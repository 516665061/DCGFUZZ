import os
import sys
import json
import time
import math
import random
import argparse
import subprocess
import numpy as np

# 导入我们刚刚写好的数学引擎和记忆库
from sgff_math import SGFFMathEngine
from sgff_memory import SGFFMemory

# === 环境配置 (保持与原版一致) ===
PX4_DIR = "/home/linux/Project/PX4-Autopilot-v1.15.4"
SEMANTIC_SPACE_FILE = "semantic_space.json"
WORKER_SCRIPT = "gsa_worker.py"
WORKER_TEMP_FILE = ".worker_temp_result.json" 
POC_FILE = "poc_combinations.json"
FUZZ_LOG_FILE = "fuzz_results.json"
BASELINE_FILE = "./backup/backup.json"

class SGFFMaster:
    def __init__(self, target_phase):
        self.target_phase = target_phase
        self.env_running = False
        self.base_err = 0.5  
        self.base_vib = 0.05 
        self.global_id = 0
        self.last_score = 10.0

        # 1. 加载语义空间
        if not os.path.exists(SEMANTIC_SPACE_FILE):
            print(f"❌ 错误：找不到 {SEMANTIC_SPACE_FILE}")
            sys.exit(1)
            
        with open(SEMANTIC_SPACE_FILE, 'r') as f:
            space_data = json.load(f)
            
        if self.target_phase not in space_data:
            print(f"❌ 错误：语义空间中不存在相位 '{self.target_phase}'")
            sys.exit(1)
            
        self.active_params = space_data[self.target_phase]['parameters']
        print(f"🎯 目标阶段: {self.target_phase} | 激活参数池: {len(self.active_params)} 个")

        baseline_data = {p_name: p_info['cur'] for p_name, p_info in self.active_params.items()}
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        with open(BASELINE_FILE, 'w') as f:
            json.dump(baseline_data, f, indent=4)
        
        self.known_crash_signatures = []
        if os.path.exists(POC_FILE):
            try:
                with open(POC_FILE, 'r') as f:
                    pocs = json.load(f)
                    for poc in pocs:
                        comb = poc.get("minimal_combination", {})
                        if comb and comb not in self.known_crash_signatures:
                            self.known_crash_signatures.append(comb)
            except Exception: pass

        # 2. 初始化 SGFF 核心模块
        self.memory = SGFFMemory(self.active_params, stagnation_limit=5)
        # 3. 物理场超参数 (Hyperparameters)
        # sigma: 引力扩散半径 (因为参数会归一化或有不同的极差，这里我们设为一个相对合理的标量，或者你可以在 math 里做空间距离归一化)
        self.sigma = 2.0 
        # eta: 基础学习率 (引力滑动步长系数，表示每次沿着梯度移动参数极差的百分比)
        self.eta = 0.15
        # 初始化数字孪生：记录飞控当前的真实参数状态
        self.current_fc_dict = {name: p['cur'] for name, p in self.active_params.items()}

    # ==========================================
    # 物理环境调度 (复用原版逻辑)
    # ==========================================
    def cleanup_procs(self):
        subprocess.run("pkill -9 -f px4", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("pkill -9 -f jmavsim", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("pkill -9 -f mavros", shell=True, stderr=subprocess.DEVNULL)
        self.env_running = False
        time.sleep(3)

    def cleanup_rootfs(self):
        rootfs_path = os.path.join(PX4_DIR, "build/px4_sitl_default/rootfs")
        if os.path.exists(rootfs_path):
            subprocess.run(f"rm -rf {rootfs_path}", shell=True)

    def start_sitl_and_mavros(self):
        self.cleanup_procs()
        self.cleanup_rootfs()
        print("\n🚀 [Master] 正在启动 PX4 SITL 与 MAVROS 仿真环境...")
        speed_factor = 3
        px4_cmd = f"cd {PX4_DIR} && PX4_SIM_SPEED_FACTOR={speed_factor} HEADLESS=1 make px4_sitl_default jmavsim"
        # px4_cmd = f"cd {PX4_DIR} && make px4_sitl_default jmavsim"
        subprocess.Popen(["gnome-terminal", "--title=PX4", "--", "bash", "-c", f"{px4_cmd}; exec bash"])
        time.sleep(10)
        mavros_cmd = "ros2 launch mavros px4.launch fcu_url:=udp://:14540@"
        subprocess.Popen(["gnome-terminal", "--title=MAVROS", "--", "bash", "-c", f"{mavros_cmd}; exec bash"])
        time.sleep(15)
        self.env_running = True
        self.current_fc_dict = {name: p['cur'] for name, p in self.active_params.items()}

    # 核心计算引擎：只提取与飞控当前真实状态不同的参数
    def _get_delta_params(self, target_dict):
        """
        核心辅助：计算目标字典与飞控当前真实状态的差异。
        如果环境未启动(env_running=False)，参考系自动切换为语义空间的 cur 基准值。
        """
        if not self.env_running:
            # 环境未启动，说明下一次 run_worker_task 会触发冷启动，参考系应为原厂基准
            reference_dict = {name: p['cur'] for name, p in self.active_params.items()}
        else:
            # 环境正在运行，参考系为内存中的数字孪生
            reference_dict = self.current_fc_dict
            
        delta = {}
        for k, v in target_dict.items():
            # 容差 1e-5，忽略微小精度抖动
            if abs(float(v) - float(reference_dict[k])) > 1e-5:
                delta[k] = v
        return delta
    
    def run_worker_task(self, task, is_calibration=False):
        if not self.env_running:
            self.start_sitl_and_mavros()
            
        if os.path.exists(WORKER_TEMP_FILE):
            os.remove(WORKER_TEMP_FILE)

        if is_calibration:
            task['id'] = "CALIB"
        else:
            self.global_id += 1
            task['id'] = self.global_id
                
        try:
            proc = subprocess.Popen(["python3", WORKER_SCRIPT, json.dumps(task), WORKER_TEMP_FILE])
            proc.wait(timeout=300) 

            if os.path.exists(WORKER_TEMP_FILE):
                try: # 👈 【必须加这层防护】
                    with open(WORKER_TEMP_FILE, 'r') as f:
                        res = json.load(f)
                except Exception as e:
                    print(f"⚠️ 读取 Worker 临时结果失败 (可能文件截断): {e}")
                    self.env_running = False
                    return 0.0, False, False, {}
                    
                status = res.get('status', '')
                if "ENV" in status or "TIMEOUT" in status or "EXCEPTION" in status:
                    self.env_running = False
                    return 0.0, False, False, {} # 环境异常
                    
                score = res.get('score', 0.0)
                crashed = res.get('crashed', False) or "CRASH" in status
                metrics = res.get('metrics', {})

                if crashed: 
                    self.env_running = False # 物理炸机，必须重置环境
                return score, crashed, True, metrics
            else:
                self.env_running = False
                return 0.0, False, False, {}
        except Exception as e:
            print(f"❌ 调度器异常: {e}")
            self.env_running = False
            return 0.0, False, False, {}
        
    def calibrate_system(self):
        print("\n🛠️  阶段 1: 系统基准校准 (测量环境底噪)...")
        err_list, vib_list = [], []
        attempts = 0
        while len(err_list) < 5 and attempts < 12:
            print(f"  [校准] 样本采集 {len(err_list)+1}/5 (尝试 {attempts+1})...")
            target_dict = {name: p['cur'] for name, p in self.active_params.items()}
            delta_params = self._get_delta_params(target_dict)
            
            task = {"mode": self.target_phase, "params": delta_params}
            # 接收 4 个返回值
            _, crashed, success, metrics = self.run_worker_task(task, is_calibration=True)
            
            if success and not crashed and metrics:
                err_list.append(metrics.get('mean_error', 0.0))
                vib_list.append(metrics.get('variance_sum', 0.0))
            else:
                print("  ⚠️ 校准样本无效 (炸机或环境错误)，跳过。")
                self.env_running = False
            attempts += 1

        if len(err_list) > 0:
            self.base_err = max(sum(err_list) / len(err_list), 0.1)
            self.base_vib = max(sum(vib_list) / len(vib_list), 0.01)
            print(f"✅ 校准完成! Base Err: {self.base_err:.4f}, Base Vib: {self.base_vib:.4f}")
        else:
            print("❌ 校准失败，请检查 PX4 环境！")
            sys.exit(1)

    def minimize_crash_poc(self, crashing_params):
        print("\n" + "!"*50)
        print("🔍 [PoC 提纯] 检测到崩溃，启动最小变异组合隔离算法...")        
        
        mutated_keys = [k for k, v in crashing_params.items() if abs(float(v) - float(self.active_params[k]['cur'])) > 1e-4]
        test_params = crashing_params.copy()

        # === 核心恢复：利用已知黑名单进行免疫过滤 ===
        hit_known_bug = False
        for known_sig in self.known_crash_signatures: 
            is_subset = True
            for k, v in known_sig.items():
                if k not in test_params or abs(float(test_params[k]) - float(v)) > 1e-4:
                    is_subset = False
                    break
            
            if is_subset:
                print(f"🕵️ 发现当前组合包含已知致毁子集: {known_sig}")
                for k in known_sig.keys():
                    test_params[k] = float(self.active_params[k]['cur'])
                    if k in mutated_keys: mutated_keys.remove(k)
                hit_known_bug = True
                
        if hit_known_bug:
            print("🧪 正在测试剔除已知 Bug 后的剩余组合是否安全...")
            test_params = self.memory.vector_to_dict(self.memory.dict_to_vector(test_params), self.active_params)
            
            delta_test = self._get_delta_params(test_params) 
            print(f"  -> 正在注入剔除旧Bug后的参数 (需注入量: {len(delta_test)})...")
            task = {"mode": self.target_phase, "params": delta_test, "base_err": self.base_err, "base_vib": self.base_vib}
            # 热重置，测一下剩余的参数
            _, crashed, success, _ = self.run_worker_task(task, is_calibration=True)
            
            if success and not crashed:
                self.current_fc_dict = test_params.copy()
                print("✅ 剩余组合安全飞行！证明本次 Crash 纯粹由旧 Bug 触发，直接跳过重复提纯。")
                print("!"*50 + "\n")
                # 虽然不记录重复 PoC，但依然要把这个坐标点变成白洞
                self.memory.blacklist_poc_crash(crashing_params)
                return 
            elif success and crashed:
                print("💥 剔除旧 Bug 后依然崩溃！发现了隐藏的新漏洞组合！继续提纯...")
                crashing_params = test_params.copy()
            else:
                return # 环境异常
        
        # ===  Delta Debugging 降维提纯 ===
        minimal_mutations = mutated_keys.copy()
        for p_test in mutated_keys:
            if len(minimal_mutations) <= 1: break 
                
            print(f"  [降维测试] 尝试将 {p_test} 恢复为基准值...")
            test_params = crashing_params.copy()
            
            # 【核心修复】：不要用 float() 强转，直接取值，然后过一遍翻译器恢复枚举约束！
            test_params[p_test] = self.active_params[p_test]['cur']
            test_params = self.memory.vector_to_dict(self.memory.dict_to_vector(test_params), self.active_params)
            
            delta_test = self._get_delta_params(test_params)
            task = {"mode": self.target_phase, "params": delta_test, "base_err": self.base_err, "base_vib": self.base_vib}
            _, crashed, success, _ = self.run_worker_task(task, is_calibration=True)
            
            if not success: continue

            self.current_fc_dict = test_params.copy()
            if crashed:
                print(f"  ✅ 剔除 {p_test} 后仍然崩溃，已移除干扰项。")
                minimal_mutations.remove(p_test)
                crashing_params = test_params.copy()
            else:
                print(f"  🛑 剔除 {p_test} 后无人机恢复安全，保留核心项。")
                
        minimal_combination = {k: crashing_params[k] for k in minimal_mutations}
        if minimal_combination not in self.known_crash_signatures:
            self.known_crash_signatures.append(minimal_combination)
        
        poc_record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "original_count": len(mutated_keys),
            "minimal_count": len(minimal_mutations),
            "minimal_combination": minimal_combination
        }
        
        pocs = []
        if os.path.exists(POC_FILE):
            try:
                with open(POC_FILE, 'r') as f: 
                    pocs = json.load(f)
            except Exception:
                print("⚠️ PoC 记录文件可能已损坏，将以新列表覆盖写入。")
        pocs.append(poc_record)
        with open(POC_FILE, 'w') as f: json.dump(pocs, f, indent=4)
        
        self.memory.blacklist_poc_crash(crashing_params)
        print(f"🔥 提纯完成！致毁核心已保存并永久禁忌化。")
        print("!"*50 + "\n")

    # ==========================================
    # SGFF 算法主轴 (论文 Algorithm 2 的具体实现)
    # ==========================================
    def initial_exploration(self, num_samples=5):
        """宇宙微波背景探测：通过随机抽样建立初始引力场"""
        print(f"\n🌌 阶段 2: 初始星系探测 (发送 {num_samples} 个随机探测器)...")
        base_params = {name: p['cur'] for name, p in self.active_params.items()}
        
        for i in range(num_samples):
            # 随机扰动 2-3 个参数
            test_params = base_params.copy()
            k = random.randint(2, min(5, self.memory.dim))
            keys_to_mutate = random.sample(list(self.active_params.keys()), k)
            
            for key in keys_to_mutate:
                p_min, p_max = float(self.active_params[key]['min']), float(self.active_params[key]['max'])
                test_params[key] = random.uniform(p_min, p_max)
                
            test_params = self.memory.vector_to_dict(self.memory.dict_to_vector(test_params), self.active_params)
            
            delta_params = self._get_delta_params(test_params)
            task = {"mode": self.target_phase, "params": delta_params, "base_err": self.base_err, "base_vib": self.base_vib}
            score, crashed, success, _ = self.run_worker_task(task)
            
            if success:
                self.current_fc_dict = test_params.copy()
                self.last_score = score
                self.memory.add_record(test_params, score)
                self.memory.save_log(FUZZ_LOG_FILE)
                if crashed:
                    self.minimize_crash_poc(test_params)

    def run_fuzz_loop(self):
        """引力坍缩主循环"""
        self.calibrate_system()
        if os.path.exists(FUZZ_LOG_FILE):
            success = self.memory.load_log(FUZZ_LOG_FILE)
            if not success or len(self.memory.memory_bank) == 0:
                self.initial_exploration(num_samples=5)
            else:
                self.last_score = self.memory.memory_bank[-1][1]
        else:
            self.initial_exploration(num_samples=5)
        
        print("\n🌀 阶段 3: 开启高维引力坍缩主循环 (SGFF Collapse Loop)...")
        
        # 初始化当前粒子坐标为系统的默认基准参数
        current_dict = {name: p['cur'] for name, p in self.active_params.items()}
        current_x = self.memory.dict_to_vector(current_dict)
        
        while True:
            print("\n" + "="*60)
            
            # --- Step 1: 感知引力梯度 (Algorithm 1) ---
            gradient = SGFFMathEngine.calculate_gravity_gradient(
                current_x, self.memory.memory_bank, self.sigma, ranges = self.memory.bounds_max - self.memory.bounds_min
            )
            
            # --- Step 2: 动态掩码与激活 (Algorithm 1) ---
            # 估计当前区域的危险程度 (如果刚被弹射出来，score 取 10.0 作为平稳基准)
            estimated_score = self.last_score if hasattr(self, 'last_score') and self.last_score > 0 else 10.0
            
            top_k_indices = SGFFMathEngine.adaptive_top_k_mask(
                gradient, estimated_score, base_score=10.0, max_dim=self.memory.dim
            )
            
            activated_names = [self.memory.param_names[i] for i in top_k_indices]
            print(f"📡 [物理引擎] 场梯度已解析。激活变异维度 (K={len(activated_names)}): {activated_names}")
            
            # --- Step 3: 引力滑动 (Algorithm 1) ---
            next_x = SGFFMathEngine.gravitational_step_update(
                current_x, gradient, top_k_indices, 
                self.memory.bounds_min, self.memory.bounds_max, self.eta
            )
            
            # 翻译回飞控字典
            next_dict = self.memory.vector_to_dict(next_x, self.active_params)
            delta_params = self._get_delta_params(next_dict)
            print(f"🛸 [飞行测试] 正在执行引力场生成的联合突变 (需变异量: {len(delta_params)})...")
            task = {"mode": self.target_phase, "params": delta_params, "base_err": self.base_err, "base_vib": self.base_vib}
            
            # 为了确保飞行纯净，每次都强行重启环境（模拟真实 Fuzzing 的重置）
            # self.env_running = False 
            
            score, crashed, success, _ = self.run_worker_task(task)
            
            if not success:
                print("⚠️ 环境异常，跳过本次结果记录。")
                self.env_running = False
                continue
            
            self.current_fc_dict = next_dict.copy()
            # --- Step 5: 更新宇宙记忆库 (Algorithm 2) ---
            print(f"📊 [结果] 物理碰撞得分: {score:.2f}")
            self.last_score = score
            self.memory.add_record(next_dict, score)
            self.memory.save_log(FUZZ_LOG_FILE)

            if crashed:
                self.minimize_crash_poc(next_dict)
                # 炸机后，不要在原地停留，恢复到基准点或随机点重新开始寻找引力井
                self.env_running = False
                current_dict = {name: p['cur'] for name, p in self.active_params.items()}
                current_x = self.memory.dict_to_vector(current_dict)
                continue
                
            # --- Step 6: 禁忌斥力逃逸检查 (Algorithm 2) ---
            repulsion_triggered = self.memory.check_and_apply_repulsion()
            
            if repulsion_triggered:
                # 被白洞排斥后，当前粒子坐标需要被随机弹射到一个新的安全区
                # 相当于加入强大的量子涨落噪声
                current_x = current_x + np.random.normal(0, (self.memory.bounds_max - self.memory.bounds_min)*0.2)
                current_x = np.clip(current_x, self.memory.bounds_min, self.memory.bounds_max)
            else:
                # 平稳继承当前坐标，继续下一轮坍缩
                current_x = next_x

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="Mission", choices=["Takeoff", "Mission", "Hold", "Landing"])
    args = parser.parse_args()
    
    master = SGFFMaster(target_phase=args.phase)
    try:
        master.run_fuzz_loop()
    except KeyboardInterrupt:
        print("\n🛑 接收到退出信号，安全停止。")
    except Exception as e:
        print(f"\n❌ 程序发生异常崩溃: {e}")
    finally:
        # 无论发生什么，死前最后一次强行落盘！
        master.memory.save_log(FUZZ_LOG_FILE) 
        master.cleanup_procs()