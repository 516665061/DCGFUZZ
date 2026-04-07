#!/usr/bin/env python3
import sys
import json
import time
import numpy as np
import math
import rclpy
import os
from rclpy.node import Node
from mavros_msgs.msg import Waypoint
from mavros_msgs.srv import WaypointPush, WaypointClear

from pathlib import Path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from HITL_FUZZ.px4_interface import PX4Interface
from HITL_FUZZ.oracle import HITLOracle

class GSAWorker(Node):
    def __init__(self, task_json, output_file):
        super().__init__("gsa_worker")
        self.output_file = output_file
        self.baseline_file = "./backup/backup.json"
        
        # 解析任务 (增加了 mode 字段)
        try:
            self.task_data = json.loads(task_json)
            self.task_id = self.task_data.get('id', -1)
            self.mode = self.task_data.get('mode', 'Mission')
            self.params = self.task_data.get('params', {})
            self.BASE_ERR = self.task_data.get('base_err', 0.5)
            self.BASE_VIB = self.task_data.get('base_vib', 0.05)
        except Exception as e:
            self.get_logger().error(f"JSON Parse Error: {e}")
            sys.exit(1)

        self.px4 = PX4Interface(self)
        self.oracle = HITLOracle(self.px4)
        
        self.wp_clear_cli = self.create_client(WaypointClear, "/mavros/mission/clear")
        self.wp_push_cli = self.create_client(WaypointPush, "/mavros/mission/push")
        
        self.takeoff_alt = 5.0
        self.freq = 20.0  
        self.rate = self.create_rate(self.freq)

    def save_result(self, result):
        """核心：将结果保存到 Master 指定的临时文件中"""
        try:
            with open(self.output_file, 'w') as f:
                json.dump(result, f)
            self.get_logger().info(f"💾 结果已写入: {self.output_file}")
        except Exception as e:
            self.get_logger().error(f"❌ 无法写入结果文件: {e}")

    def backup_baseline(self):
        """【核心修复 1】增量备份：确保每次遇到的新参数都能被记录"""
        baseline_data = {}
        space_file = "semantic_space.json"
        param_meta = {}
        # 1. 加载参数定义空间（为了获取精度定义 decimal）
        if os.path.exists(space_file):
            with open(space_file, 'r') as f:
                space_data = json.load(f)
                # 扁平化处理，方便查找
                for group in space_data.values():
                    for p_name, p_info in group.items():
                        param_meta[p_name] = p_info
        # 2. 尝试读取已存在的基准文件
        if os.path.exists(self.baseline_file):
            try:
                with open(self.baseline_file, 'r') as f:
                    baseline_data = json.load(f)
            except json.JSONDecodeError:
                pass # 如果文件损坏则重新创建
        
        for p_name in self.params.keys():
            baseline_data[p_name] = param_meta[p_name].get("cur")
        
        os.makedirs(os.path.dirname(self.baseline_file), exist_ok=True)
        with open(self.baseline_file, 'w') as f:
            json.dump(baseline_data, f, indent=4)
        self.get_logger().info(f"✅ 增量备份已更新至: {self.baseline_file}")

        # need_save = False
        # for p_name in self.params.keys():
        #     if p_name not in baseline_data:
        #         # 2. 如果是新参数，去飞控里读取它的默认值（带重试机制）
        #         val = None
        #         for _ in range(5):
        #             val = self.px4.get_param(p_name)
        #             if val is not None:
        #                 break
        #             time.sleep(0.5)
                
        #         if val is not None:
        #             # --- 核心修复：根据元数据进行精度修约 ---
        #             if p_name in param_meta:
        #                 decimal = param_meta[p_name].get("decimal")
        #                 p_type = param_meta[p_name].get("type")
                        
        #                 if p_type == "Int32":
        #                     val = int(round(val))
        #                 elif decimal is not None:
        #                     val = round(float(val), decimal)
        #                 else:
        #                     val = round(float(val), 4) # 默认兜底 4 位
                    
        #             baseline_data[p_name] = val
        #             need_save = True
        #             self.get_logger().info(f"📥 修正备份: {p_name} -> {val}")
                    
        # # 3. 只有当发现了新参数时才重新写入文件
        # if need_save:
        #     os.makedirs(os.path.dirname(self.baseline_file), exist_ok=True)
        #     with open(self.baseline_file, 'w') as f:
        #         json.dump(baseline_data, f, indent=4)
        #     self.get_logger().info(f"✅ 增量备份已更新至: {self.baseline_file}")

    def save_current_params_list(self):
        """将本次任务修改的参数名保存到本地，供下一个任务恢复使用"""
        last_params_file = ".last_params_list.json"
        try:
            with open(last_params_file, 'w') as f:
                # 只记录参数名（key）即可
                json.dump(list(self.params.keys()), f)
        except Exception as e:
            self.get_logger().error(f"无法保存参数追踪文件: {e}")

    def restore_baseline(self):
        """读取上一次任务修改过的参数名，并从基准文件中恢复它们"""
        last_params_file = ".last_params_list.json"
        
        if not os.path.exists(last_params_file):
            self.get_logger().info("📭 未发现上一次任务的残留参数记录，跳过恢复。")
            return
        
        if not os.path.exists(self.baseline_file):
            self.get_logger().error("⚠️ 恢复失败：找不到备份文件！")
            return

        try:
            # 1. 加载上回改了哪些参数
            with open(last_params_file, 'r') as f:
                last_modified_names = json.load(f)
            
            if not last_modified_names:
                return

            # 2. 加载基准值
            with open(self.baseline_file, 'r') as f:
                baseline_data = json.load(f)

            self.get_logger().info(f"♻️ 检测到上一次任务修改了 {len(last_modified_names)} 个参数，正在恢复至基准值...")
            
            # 3. 逐个恢复
            for p_name in last_modified_names:
                if p_name in baseline_data:
                    self.px4.set_param(p_name, baseline_data[p_name])
                else:
                    self.get_logger().warn(f"⚠️ 基准文件中缺少参数 {p_name}，无法完全恢复。")
            self.get_logger().info("✅ 上一次任务的残留参数已清理完毕。")
            # 4. 清理追踪文件，防止重复恢复
            os.remove(last_params_file)
        except Exception as e:
            self.get_logger().error(f"❌ 恢复上一次参数时出错: {e}")
        # 给参数生效留一点缓冲时间
        time.sleep(1.0)

    def upload_square_mission(self):
        """上传以起飞点为基准的方形航线任务"""
        self.get_logger().info("🛰️ 正在生成并上传方形飞行任务 (边长 20m)...")
        
        while not self.wp_clear_cli.wait_for_service(timeout_sec=1.0):
            pass
        self.wp_clear_cli.call_async(WaypointClear.Request())
        time.sleep(0.5)

        home_lat = self.px4.global_pos.latitude
        home_lon = self.px4.global_pos.longitude

        # 计算米到经纬度的转换系数
        lat_deg_per_m = 1.0 / 111320.0
        lon_deg_per_m = 1.0 / (111320.0 * math.cos(math.radians(home_lat)))

        # ENU 格式
        offsets = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)]
        wl = []
        for i, (dx, dy) in enumerate(offsets):
            w = Waypoint()
            w.frame = Waypoint.FRAME_GLOBAL_REL_ALT
            w.command = 16 # # MAV_CMD_NAV_WAYPOINT
            w.is_current = (i == 0)
            w.autocontinue = True
            w.x_lat = home_lat + (dy * lat_deg_per_m) # ROS Y -> North
            w.y_long = home_lon + (dx * lon_deg_per_m) # ROS X -> East
            w.z_alt = float(self.takeoff_alt)
            wl.append(w)

        req = WaypointPush.Request()
        req.waypoints = wl
        self.wp_push_cli.call_async(req)
        time.sleep(1.0)
        # 【修复】：获取生成航线时的当前局部坐标
        curr_p = self.px4.pose.pose.position

        # 更新 Oracle 的目标用于偏航判定
        self.oracle.target_waypoints = [
            {'x': curr_p.x + dx, 'y': curr_p.y + dy, 'z': self.takeoff_alt} 
            for dx, dy in offsets
        ]
        self.oracle.current_wp_index = 0 

    def get_precision(self, value):
        """
        动态获取任务中定义的参数精度
        例如: 1.0 -> 1位, 0.53 -> 2位, 1 -> 0位
        """
        str_val = str(value)
        if '.' in str_val:
            return len(str_val.split('.')[1])
        return 0
    
    def inject_and_verify_params(self):       
        """
        闭环注入逻辑：设置参数 -> 等待同步 -> 回读验证 -> 返回真实运行值
        """
        if not self.params or len(self.params) == 0:
            self.get_logger().info("⚠️ 本次任务没有需要注入的参数，跳过注入阶段。")
            return {}
        
        self.get_logger().info(f"💉 正在注入突变参数: {[(k, v) for k, v in self.params.items()]}")
        actual_injected = {}
        
        for name, target_value in self.params.items():
            # 1. 识别任务中该参数的原始精度
            precision = self.get_precision(target_value)

            # 2. 注入参数
            success = self.px4.set_param(name, target_value)
            time.sleep(0.05) # 给飞控物理写入的时间
            
            # 3. 回读 PX4 内部的原始真值 (如 0.52999997138)
            raw_actual = self.px4.get_param(name)
            
            if raw_actual is not None:
                # 4. 关键：按照任务要求的精度进行动态修约
                if precision == 0:
                    cleaned_val = int(round(raw_actual))
                else:
                    cleaned_val = round(float(raw_actual), precision)
                
                if cleaned_val != target_value and round(float(raw_actual), 6) != round(float(target_value), 6):
                    self.get_logger().warn(f"⚠️ 参数 {name} 注入后回读值与目标不符！目标={target_value}，回读={cleaned_val}")
                # else:
                #     self.get_logger().info(f"✅ 参数 {name} 注入成功且回读验证通过: {cleaned_val}")
                actual_injected[name] = cleaned_val
            else:
                self.get_logger().error(f"❌ 无法从 PX4 获取参数 {name} 的回读值")
        return actual_injected

    def run_experiment(self):
        result = {
            "id": self.task_id, "mode": self.mode, "params": {},
            "metrics": {"mean_error": 0.0, "variance_sum": 0.0, "survival_time": 0.0, "total_duration": 0.0},
            "score": 0.0, "status": "UNKNOWN", "crashed": False
        }
        
        post_injection_log = []
        
        try:
            self.px4.wait_services()
            wait_start = time.time()
            while not self.px4.state.connected:
                if time.time() - wait_start > 30:
                    result["status"] = "TIMEOUT_CONNECT"
                    return
                time.sleep(0.5)


            # self.restore_baseline()
            # self.backup_baseline()
            time.sleep(1.0)

            # --- 核心修复：根据相位分离执行流 ---
            self.get_logger().info(f"🚀 开始执行 {self.mode} 阶段测试序列...")


            if self.mode == "Takeoff":
                # 【起飞模式专属逻辑】：地面注入 -> 起飞 -> 监控
                self.px4.set_mode("STABILIZED")
                time.sleep(1.0)
                self.save_current_params_list()

                # 起飞前设置一个固定的目标点，帮助 Oracle 进行初始的偏航和飞逃监控（如果不设置，Oracle 在起飞阶段可能会因为没有参考点而误判）
                curr_p = self.px4.pose.pose.position
                self.oracle.target_waypoints = [{'x': curr_p.x, 'y': curr_p.y, 'z': self.takeoff_alt}]
                self.oracle.current_wp_index = 0

                # 在地面注入，此时如果参数离谱可能会直接导致解锁失败
                actual_params = self.inject_and_verify_params()
                result["params"] = actual_params
                
                # 记录开始时间并触发起飞
                start_test_time = time.time()
                test_duration = 15.0
                result["metrics"]["total_duration"] = test_duration
                
                if not self.px4.auto_takeoff(target_alt=self.takeoff_alt):
                    result["status"] = "CRASH_TAKEOFF_PARAMS"
                    return
            
            else:
                # 【空中模式专属逻辑 (Mission/Hold/Landing)】：默认起飞 -> 空中稳定 -> 注入 -> 执行
                if not self.px4.auto_takeoff(target_alt=self.takeoff_alt):
                    result["status"] = "ENV_ERROR_TAKEOFF" # 这种算环境异常，不扣参数的分
                    return
                
                self.get_logger().info("⏳ 等待飞行器在空中悬停稳定...")
                time.sleep(5.0) 
                if self.params is not None and len(self.params) > 0:
                    self.save_current_params_list()
                    
                    # 空中注入突变参数
                    actual_params = self.inject_and_verify_params()
                    result["params"] = actual_params
                    # actual_params = {} # 先不注入参数，直接测试飞行稳定性，看看是否能成功完成任务
                    # if len(actual_params) == 0:
                    #     result["status"] = "INJECTION_FAILED"
                    #     return
            
                if self.mode == "Mission":
                    self.upload_square_mission()
                    self.px4.set_mode("AUTO.MISSION")
                    test_duration = 40.0
                elif self.mode == "Hold":
                    curr_p = self.px4.pose.pose.position
                    self.oracle.target_waypoints = [{'x': curr_p.x, 'y': curr_p.y, 'z': curr_p.z}]
                    self.oracle.current_wp_index = 0
                    self.px4.set_mode("AUTO.LOITER")
                    test_duration = 20.0
                elif self.mode == "Landing":
                    curr_p = self.px4.pose.pose.position
                    self.oracle.target_waypoints = [{'x': curr_p.x, 'y': curr_p.y, 'z': 0.0}]
                    self.oracle.current_wp_index = 0
                    self.px4.set_mode("AUTO.LAND")
                    test_duration = 20.0
                    
                result["metrics"]["total_duration"] = test_duration
                start_test_time = time.time()

            takeoff_max_z = self.px4.pose.pose.position.z if self.mode == "Takeoff" else 0.0
            landing_min_z = self.px4.pose.pose.position.z if self.mode == "Landing" else float('inf')
            # --- Step 5: 轨迹与姿态监控 ---
            while time.time() - start_test_time < test_duration:
                curr_pos = self.px4.pose.pose.position
                
                # 【核心修复】：动态 Z 轴锚定算法 (棘轮机制)
                if self.mode == "Takeoff":
                    # 目标高度只增不减：正常爬升无惩罚，掉高度或超调产生惩罚
                    takeoff_max_z = max(takeoff_max_z, curr_pos.z)
                    self.oracle.target_waypoints[0]['z'] = min(takeoff_max_z, self.takeoff_alt)
                elif self.mode == "Landing":
                    # 目标高度只减不增：正常下降无惩罚，触地反弹产生惩罚
                    landing_min_z = min(landing_min_z, curr_pos.z)
                    self.oracle.target_waypoints[0]['z'] = max(landing_min_z, 0.0)
                    
                is_crashed, reason = self.oracle.check_health()
                
                curr_ang_vel = self.px4.vel.twist.angular
                curr_pos = self.px4.pose.pose.position
                err = self.oracle._calculate_route_deviation(curr_pos)
                # 记录核心震荡指标
                post_injection_log.append({
                    "err": err, 
                    "ang_vel": [curr_ang_vel.x, curr_ang_vel.y, curr_ang_vel.z]
                })

                if is_crashed:
                    if self.mode == "Takeoff" and "Ground Collision" in reason and time.time() - start_test_time < 2.0:
                        pass # 刚起飞时高度低，Oracle 可能会误报触地
                    else:
                        self.get_logger().error(f"💥 真实飞行崩溃: {reason}")
                        result["status"] = f"CRASH: {reason}"
                        result["crashed"] = True
                        result["metrics"]["survival_time"] = time.time() - start_test_time
                        result["score"] = self.calculate_final_score(result, post_injection_log, crashed=True)
                        return

                self.rate.sleep()

            # --- 任务圆满完成 ---
            # result["status"] = "SUCCESS" if len(actual_params) == len(self.params) else "PARTIAL_SUCCESS"
            result["status"] = "SUCCESS"
            result["metrics"]["survival_time"] = time.time() - start_test_time
            result["score"] = self.calculate_final_score(result, post_injection_log, crashed=False)
            self.get_logger().info(f"✅ Result: Score={result['score']:.4f} (Err={result['metrics']['mean_error']:.3f}, Var={result['metrics']['variance_sum']:.5f})")

        except Exception as e:
            self.get_logger().error(f"Worker Error: {e}")
            result["status"] = f"EXCEPTION: {str(e)}"
        finally:
            if self.px4.state.connected:
                self.px4.land()
            #self.restore_baseline()
            self.save_result(result)
            time.sleep(1)

    def calculate_final_score(self, result, post_injection_log, crashed):
        """
        科学评分逻辑
        """
        # 基础权重配置
        W_ERROR = 10.0      # 跟踪误差权重
        W_VIB = 20.0       # 震荡惩罚权重
        
        if not post_injection_log:
            return 10.0

        # 计算原始指标
        mean_err = np.mean([d['err'] for d in post_injection_log])
        pqr_data = np.array([d['ang_vel'] for d in post_injection_log])
        sum_var = np.sum(np.var(pqr_data, axis=0))

        # 记录到 metrics 中
        result["metrics"]["mean_error"] = float(mean_err)
        result["metrics"]["variance_sum"] = float(sum_var)

        norm_err = mean_err / (self.BASE_ERR + 1e-6)
        norm_var = sum_var / (self.BASE_VIB + 1e-6)

        # 计算得分
        return (W_ERROR * norm_err) + (W_VIB * norm_var)
        
        
def main():

    rclpy.init()
    if len(sys.argv) < 3: 
        print("Usage: gsa_worker.py <task_json> <output_file>")
        return
    
    task_json = sys.argv[1]
    output_file = sys.argv[2]
    worker = GSAWorker(task_json, output_file)
    worker.run_experiment()
    
    worker.px4.stop() # 停止接口线程
    worker.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()