# fuzzer.py
import time
import json
import random
import threading
from copy import deepcopy
from rclpy.node import Node

# 自定义模块
from px4_interface import PX4Interface
from mutation import Mutator
from oracle import HITLOracle
from utils import JsonLogger, timestamp
from mission_commander import MissionCommander

class PX4Fuzzer(Node):
    def __init__(self, args):
        super().__init__("px4_fuzzer")
        self.args = args

        # 1. 初始化核心组件
        # 注意：PX4Interface 现在内部会启动后台线程 spin，确保数据实时更新
        self.px4 = PX4Interface(self) 
        self.oracle = HITLOracle(self.px4)
        self.mission_cmd = MissionCommander(self.px4)

        # 2. 加载配置
        with open(args.config, "r") as f:
            self.param_cfg = json.load(f)
        
        # 3. 初始化变异器与种群
        self.mutator = Mutator(self.param_cfg, strategy=args.strategy)
        self.monitored_params = list(self.param_cfg.keys())
        random.seed(args.seed)

        # 4. 日志记录器
        self.fuzz_log = JsonLogger(f"{args.outdir}/fuzz_log.json")
        self.crash_log = JsonLogger(f"{args.outdir}/crash_log.json")
        self.event_log = JsonLogger(f"{args.outdir}/event_log.json")

        # 5. 监控控制信号
        self.monitoring = False          # 是否处于危险操作期间（需要监控）
        self.crash_event = threading.Event()
        self.stop_monitor_event = threading.Event()
        self.crash_details = None

        # 6. 启动后台监控线程
        self.monitor_thread = threading.Thread(target=self._monitor_worker, daemon=True)
        self.monitor_thread.start()

    def _monitor_worker(self):
        """
        后台监控线程：
        以较高频率询问 Oracle "现在健康吗？"
        如果不健康，立即设置 crash_event 信号，打断主线程。
        """
        while not self.stop_monitor_event.is_set():
            if self.monitoring:
                # 调用 Oracle 进行判定 (Oracle 内部读取 Interface 的原子数据)
                is_crash, reason = self.oracle.check_health()
                
                if is_crash:
                    self.get_logger().error(f"[Monitor] CRASH DETECTED: {reason}")
                    self.crash_details = {
                        "reason": reason,
                        "timestamp": timestamp(),
                        "telemetry": str(self.px4.get_data()) # 记录当时快照
                    }
                    self.crash_event.set()
                    self.monitoring = False # 触发后立即停止监控，防止重复报警
            
            # 监控频率 10Hz 足够
            time.sleep(0.1)

    def run(self):
        """主执行入口"""
        # 等待服务就绪
        self.px4.wait_services()
        
        # 获取基准参数（Baseline）
        self.get_logger().info("Snapshotting baseline parameters...")
        self.baseline = self.px4.snapshot_params(self.monitored_params)
        self.population = [deepcopy(self.baseline)]

        # 自动起飞
        if not self.start_evaluation_scenario():
            self.get_logger().error("Failed to start scenario. Skipping this cycle.")
            return

        self.get_logger().info("Fuzzer Loop Started")
        
        # 开始 Batch 循环
        # stop_all = False
        # total_iters = self.args.batch * self.args.iterations_per_run
        # current_iter = 0

        # for batch in range(self.args.batch):
        #     if stop_all: break
        #     self.get_logger().info(f"=== Batch {batch+1} Started ===")
            
        #     for i in range(self.args.iterations_per_run):
        #         current_iter += 1
        #         if not self.run_single_iteration(batch, i):
        #             # 如果 run_single_iteration 返回 False，说明发生了不可恢复的错误或用户配置停止
        #             stop_all = True
        #             break
        
        self.cleanup()

    def run_single_iteration(self, batch_id, iter_id):
        """执行单个 Fuzz 迭代"""
        iter_name = f"b{batch_id}_i{iter_id}"
        self.get_logger().info(f"--- Iteration {iter_name} ---")

        # 1. 变异 (Mutation)
        parent = random.choice(self.population)
        child = self.mutator.mutate(parent, self.args.k)
        
        # 计算差异（只应用改变了的参数）
        diff = {k: v for k, v in child.items() if parent.get(k) != v}
        if not diff:
            self.get_logger().info("Mutation produced no changes, skipping.")
            return True

        # 2. 准备阶段
        self.crash_event.clear()
        self.crash_details = None
        self.monitoring = True  # 开启监控

        # 3. 应用参数 (Apply)
        applied = {}
        for name, val in diff.items():
            # 写入前检查是否已 Crash
            if self.crash_event.is_set(): 
                break
            
            success = self.px4.set_param(name, val)
            if success:
                applied[name] = val
                self.get_logger().info(f"Set {name} = {val}")
            else:
                self.get_logger().warn(f"Failed to set {name}")
            
            time.sleep(self.args.set_delay)

        # 4. 评估阶段 (Evaluation Window)
        # 即使这里 sleep，后台的 px4_interface 依然在更新数据，monitor_thread 依然在检查
        end_time = time.time() + self.args.eval_time
        while time.time() < end_time:
            if self.crash_event.is_set():
                break
            time.sleep(0.1)

        # 5. 结束监控
        self.monitoring = False

        # 6. 结果处理
        if self.crash_event.is_set():
            # === 处理 Crash ===
            self.handle_crash(iter_name, child, applied)
            
            if not self.args.continue_on_crash:
                self.get_logger().info("Stopping due to crash (continue-on-crash=False).")
                return False # 停止整个 Fuzz
            else:
                return self.recover_vehicle() # 尝试恢复并继续
        else:
            # === 正常通过 ===
            self.get_logger().info("Pass (Stable)")
            # 记录成功日志
            self.fuzz_log.append({
                "iter": iter_name,
                "result": "pass",
                "applied": applied,
                "full_params": child
            })
            # 只有存活下来的配置才有资格进入下一代种群
            self.population.append(child)
            # 限制种群大小，防止无限增长
            if len(self.population) > 20:
                self.population.pop(0)
            
            time.sleep(self.args.loop_delay)
            return True

    def handle_crash(self, iter_name, intent_params, applied_params):
        """记录 Crash 现场"""
        self.get_logger().warn("Handling Crash...")
        
        # 获取最新的参数快照（可能因为 crash 导致部分参数没写进去，或者发生了改变）
        try:
            post_snapshot = self.px4.snapshot_params(self.monitored_params)
        except Exception:
            post_snapshot = {}

        report = {
            "iter": iter_name,
            "crash_time": timestamp(),
            "reason": self.crash_details.get("reason", "unknown"),
            "telemetry_at_trigger": self.crash_details.get("telemetry"),
            "intent_params": intent_params,
            "applied_params_before_crash": applied_params,
            "final_params_snapshot": post_snapshot
        }
        
        self.crash_log.append(report)
        self.get_logger().info(f"Crash case saved for iteration {iter_name}")

    def recover_vehicle(self):
        """恢复车辆状态以便继续 Fuzz"""
        if not self.args.restore:
            return False # 如果不配置恢复，crash 后就无法继续

        self.get_logger().info("RECOVERING: Restoring baseline parameters...")
        # 1. 恢复参数
        for name, val in self.baseline.items():
            self.px4.set_param(name, val)
        
        # 2. 确保安全（降落或复位）
        # 如果还在天上，先降落；如果已经坠地，可能需要重启 SITL（这里仅做 HITL 级别的复位尝试）
        if self.px4.state.armed:
            self.get_logger().info("Vehicle armed, forcing Land/RTL...")
            self.px4.set_mode("AUTO.LAND")
            time.sleep(10.0) # 等待降落

        # 3. 重新起飞
        # 3. 调用统一的场景启动逻辑 (替换你刚才问的那段代码)
        if self.args.auto != "None":
            self.get_logger().info(f"Re-starting {self.args.auto} scenario...")
            time.sleep(2.0) # 给姿态解算收敛时间
            
            if self.start_evaluation_scenario():
                self.get_logger().info("Recovery successful.")
                return True
            else:
                self.get_logger().error("Recovery failed to restart scenario.")
                return False
        return True

    def start_evaluation_scenario(self):
        """根据配置决定是起飞悬停还是执行航点任务"""
        self.get_logger().info("Waiting for GPS lock...")
        wait_start = time.time()
        while self.px4.global_pos is None:
            if time.time() - wait_start > 15.0:
                self.get_logger().error("Timeout waiting for GPS lock!")
                return False
            time.sleep(0.5)
        
        if self.args.auto == "Mission":
            self.get_logger().info("Initialization: Starting Mission Scenario...")
            
            # 1. 上传任务
            if not self.mission_cmd.upload_mission(alt=self.args.takeoff_alt):
                self.get_logger().error("Initialization: Mission upload failed.")
                return False
            
            # 等待一小会儿让 PX4 内部状态同步
            time.sleep(1.0) 

            # 2. 切换模式
            self.get_logger().info("Initialization: Setting Mode to AUTO.MISSION...")
            if not self.px4.set_mode("AUTO.MISSION"):
                self.get_logger().error("Initialization: Failed to set AUTO.MISSION mode (Check GPS lock?).")
                return False
            
            # 3. 解锁
            self.get_logger().info("Initialization: Arming vehicle...")
            if not self.px4.arm(True):
                self.get_logger().error("Initialization: Arming failed.")
                return False
                
            return True

        elif self.args.auto == "Hold":
            self.get_logger().info(f"Auto Takeoff to {self.args.takeoff_alt}m...")
            return self.px4.auto_takeoff(self.args.takeoff_alt)
        
        return True # 如果是 None 则认为不需要自动执行
    
    def cleanup(self):
        """清理资源"""
        self.stop_monitor_event.set()
        if self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=1.0)
        
        # 恢复默认参数
        if self.args.restore:
            self.get_logger().info("Final Restore of Baseline Parameters...")
            for name, val in self.baseline.items():
                self.px4.set_param(name, val)
        
        # 停止 Interface
        self.px4.stop()