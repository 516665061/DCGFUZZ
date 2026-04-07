import subprocess
import json
import time
import os
import sys
import random
import argparse
import math


# === 用户配置 ===
PX4_DIR = "/home/linux/Project/PX4-Autopilot-v1.15.4"
SEMANTIC_SPACE_FILE = "semantic_space.json"
FUZZ_LOG_FILE = "fuzz_results.json"
SENSITIVITY_REPORT = "online_sensitivity.json"
WORKER_SCRIPT = "gsa_worker.py"
WORKER_TEMP_FILE = ".worker_temp_result.json" 
BASELINE_FILE = "./backup/backup.json"
EFFICIENCY_FILE = "research_efficiency.json"
POC_FILE = "poc_combinations.json"

class FuzzMaster:
    def __init__(self, target_phase):
        self.target_phase = target_phase
        self.env_running = False
        self.base_err = 0.5  # 初始默认值
        self.base_vib = 0.05 # 初始默认值

        # 新增：绝对自增 ID 与效率统计变量
        self.global_id = 0
        self.start_time = time.time()
        self.total_physical_runs = 0
        self.unique_crashes_found = 0
        self.known_crash_signatures = set() # 崩溃黑名单

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
        print(f"✅ 增量备份已更新至: {BASELINE_FILE}")

        # 2. 初始化在线敏感度统计 (Welford's Algorithm 数据结构)
        self.stats = {
            name: {'count': 0, 'mu_star': 0.0, 'M2': 0.0, 'sigma': 0.0, 'crash_count': 0, 'pos_gain': 0.0, 'neg_gain': 0.0} 
            for name in self.active_params.keys()
        }
        self.total_perturbations = 0
        self.results = []
        self.ema_alpha = 0.2 # EMA 衰减系数，通常设在 0.1~0.3

        # 恢复敏感度状态与历史结果
        self._restore_state()
        self._load_known_signatures()

    def _restore_state(self):
        if os.path.exists(SENSITIVITY_REPORT):
            try:
                with open(SENSITIVITY_REPORT, 'r') as f:
                    loaded_stats = json.load(f)
                    for k, v in loaded_stats.items():
                        if k in self.stats: self.stats[k].update(v)
                self.total_perturbations = sum([s['count'] for s in self.stats.values()])
                print(f"✅ 成功从 {SENSITIVITY_REPORT} 恢复在线敏感度记忆！")
            except Exception as e: pass

        if os.path.exists(FUZZ_LOG_FILE):
            try:
                with open(FUZZ_LOG_FILE, 'r') as f:
                    self.results = json.load(f)
                # 修复 ID 错乱：从历史记录中寻找最大的 ID
                if self.results:
                    self.global_id = max([r.get('id', 0) for r in self.results])
                print(f"🔄 发现历史记录，下一个任务 ID 将从 {self.global_id + 1} 开始。")
            except: pass
    
    def _load_known_signatures(self):
        """加载已知的致毁参数组合字典，用于子集过滤"""
        self.known_crash_signatures = [] # 改为列表存储字典
        if os.path.exists(POC_FILE):
            try:
                with open(POC_FILE, 'r') as f:
                    pocs = json.load(f)
                    for poc in pocs:
                        comb = poc.get("minimal_combination", {})
                        if comb and comb not in self.known_crash_signatures:
                            self.known_crash_signatures.append(comb)
                self.unique_crashes_found = len(pocs)
                print(f"🛡️ 已加载 {len(self.known_crash_signatures)} 个已知致毁组合，将用于 PoC 免疫过滤。")
            except: pass
    
    def save_final_report(self):
        """强制落盘保存敏感度数据"""
        try:
            with open(SENSITIVITY_REPORT, 'w') as f:
                json.dump(self.stats, f, indent=4)
            print(f"💾 在线敏感度数据已安全落盘至: {SENSITIVITY_REPORT}")
        except Exception as e:
            print(f"❌ 数据保存失败: {e}")
    
    def save_efficiency_report(self):
        """记录研究用效率指标"""
        elapsed_hours = (time.time() - self.start_time) / 3600.0
        metrics = {
            "total_time_hours": round(elapsed_hours, 4),
            "total_physical_runs": self.total_physical_runs,
            "unique_vulnerabilities": self.unique_crashes_found,
            "executions_per_hour": round(self.total_physical_runs / elapsed_hours, 2) if elapsed_hours > 0 else 0
        }
        with open(EFFICIENCY_FILE, 'w') as f:
            json.dump(metrics, f, indent=4)
    
    # 3. 带有衰减式 Crash 奖励的安全约束 UCB (C-UCB)
    def get_c_ucb_targets(self, k=3):
        scores = {}
        total_n = sum([s['count'] for s in self.stats.values()]) + 1

        # 获取当前最大值用于归一化，防止量纲差异
        all_mu = [s['mu_star'] for s in self.stats.values()]
        all_sigma = [s['sigma'] for s in self.stats.values()]
        max_mu = max(all_mu) if any(all_mu) else 1.0
        max_sigma = max(all_sigma) if any(all_sigma) else 1.0
        
        for name, stat in self.stats.items():
            if stat['count'] == 0:
                # 强制覆盖：没测过的参数优先级无限大
                scores[name] = float('inf') 
            else:
                # 均值代表已知破坏力，标准差代表在组合环境下的交互潜力 (Interaction)
                # kappa=0.5 鼓励去探索那些“波动大”的参数，它们往往隐藏着深度交互漏洞
                exploitation = (stat['mu_star'] / max_mu) + 0.5 * (stat['sigma'] / max_sigma)
                exploration = math.sqrt(2 * math.log(total_n) / stat['count'])
                # 衰减式 Crash 奖励：发现崩溃时给予极高权重引导，但反复在此炸机会迅速衰减，迫使跳出局部最优
                crash_bonus = 10.0 / (1.0 + stat['crash_count']) if stat['crash_count'] > 0 else 0.0

                scores[name] = exploitation + exploration + crash_bonus
                
        # 选出得分最高的 K 个参数准备构建轨迹
        return sorted(scores, key=scores.get, reverse=True)[:k]


    # 5. Welford 在线方差更新算法(更新统计量并记录参数使系统恶化的方向梯度)
    def update_online_gsa(self, param_name, ee, score_delta, direction, total_delta_so_far):
        """
        动态贡献权重
        :param total_delta_so_far: 这一路轨迹走来，相对于 Base 的总分差
        """
        stat = self.stats[param_name]
        
        # --- 1. 计算贡献权重 (Scientific Weighting) ---
        # 如果当前步产生的变化 score_delta 占了总变化很大比例，说明归因非常准确
        contribution = abs(score_delta) / (abs(total_delta_so_far) + 1e-9)
        # 使用 sigmoid 或线性映射，将权重映射到 [0.2, 1.0] 之间
        # 即使环境很脏，也保留 0.2 的底分，防止统计停滞
        dynamic_weight = 0.2 + 0.8 * min(1.0, contribution * 2)

        reward = max(0, score_delta) * dynamic_weight # 只奖励增加了不稳定性的操作
        if direction > 0:
            stat['pos_gain'] = (1 - self.ema_alpha) * stat['pos_gain'] + self.ema_alpha * reward
        else:
            stat['neg_gain'] = (1 - self.ema_alpha) * stat['neg_gain'] + self.ema_alpha * reward
        

        # --- 3. Welford 算法在线更新 EE 的二阶统计量 ---
        # 应用动态权重更新统计量
        stat['count'] += 1
        weighted_ee = ee * dynamic_weight
        self.total_perturbations += 1

        diff = weighted_ee - stat['mu_star']
        stat['mu_star'] += diff / stat['count']
        stat['M2'] += diff * (weighted_ee - stat['mu_star'])
        
        if stat['count'] > 1:
            stat['sigma'] = math.sqrt(stat['M2'] / (stat['count'] - 1))

        # 5. 实时保存
        with open(SENSITIVITY_REPORT, 'w') as f:
            json.dump(self.stats, f, indent=4)
    
    # 7. 智能突变策略：结合边界测试与敏感度梯度
    def mutate_single_param_smart(self, current_params, p_name):
        p_info = self.active_params[p_name]
        p_min_orig, p_max_orig = float(p_info['min']), float(p_info['max'])
        p_range_orig = p_max_orig - p_min_orig
        expansion_factor = 0.2 
        p_min = p_min_orig - (p_range_orig * expansion_factor)
        p_max = p_max_orig + (p_range_orig * expansion_factor)

        if p_min_orig >= 0:
            p_min = max(0.0, p_min)
        p_range = p_max - p_min
        old_val = float(current_params[p_name])
        
        stat = self.stats[p_name]
        # 使用 Softmax 思想在两个方向间选择
        pos_exp = math.exp(stat['pos_gain'])
        neg_exp = math.exp(stat['neg_gain'])
        prob_pos = pos_exp / (pos_exp + neg_exp + 1e-9)
        
        direction = 1 if random.random() < prob_pos else -1
        
        strategy = random.random()
        
        # 极值与边界探测
        if strategy < 0.15:
            new_val = p_max
            direction = 1 if p_max > old_val else -1
        elif strategy < 0.30:
            new_val = p_min
            direction = -1 if p_min < old_val else 1
        elif strategy < 0.40 and p_min <= 0.0 <= p_max:
            new_val = 0.0  # 零值边界（触发除零异常的高危点）
            direction = -1 if old_val > 0 else 1
        else:
            # 顺着让飞机不稳定的梯度方向持续施压 
            step = p_range * random.uniform(0.15, 0.3)
            new_val = old_val + (direction * step)
            
            # 撞墙反弹
            if new_val > p_max or new_val < p_min:
                new_val = old_val - (direction * step)
                direction *= -1

        new_val = max(p_min, min(p_max, new_val))
        
        if p_info.get('type') == 'Int32':
            new_val = int(round(new_val))
        else:
            decimal = p_info.get('decimal', 4)
            new_val = round(new_val, decimal) if decimal is not None else round(new_val, 4)
            
        norm_delta = abs(new_val - old_val) / (p_range + 1e-9)
        return new_val, norm_delta, direction
    
    # 8. 系统基准校准，获取底噪水平
    def calibrate_system(self):
        """
        第一阶段：系统校准
        直接运行当前值 (cur)，测量环境底噪
        """
        print("\n🛠️  阶段 1: 系统基准校准 (测量环境底噪)...")
        err_list, vib_list = [], []
        attempts = 0
        # 采集 3 次样本以求平均，过滤掉随机干扰
        while len(err_list) < 5 and attempts < 12:
            print(f"  [校准] 样本采集 {len(err_list)+1}/5 (尝试 {attempts+1})...")
            # 此时不传 base_err，让 Worker 返回原始 metrics
            task = {
                "mode": self.target_phase, 
                "params": {name: p['cur'] for name, p in self.active_params.items()}
            }
            # run_worker_task 需要返回 (score, crashed, success, metrics)
            _, crashed, success, metrics = self.run_worker_task({}, is_calibration=True)
            
            if success and not crashed:
                err_list.append(metrics['mean_error'])
                vib_list.append(metrics['variance_sum'])
            else:
                print("  ⚠️ 校准样本无效 (炸机或环境错误)，跳过。")
                self.env_running = False # 强制下次重置
            attempts += 1

        if len(err_list) > 0:
            self.base_err = max(sum(err_list) / len(err_list), 0.1)
            self.base_vib = max(sum(vib_list) / len(vib_list), 0.01)
            print(f"✅ 校准完成! Base Err: {self.base_err:.4f}, Base Vib: {self.base_vib:.4f}")
        else:
            print("❌ 校准失败，请检查 PX4 环境！")
            exit(1)
    
    def cleanup_procs(self):
        subprocess.run("pkill -9 -f px4", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("pkill -9 -f jmavsim", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("pkill -9 -f mavros", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("pkill -9 -f java", shell=True, stderr=subprocess.DEVNULL)
        self.env_running = False
        time.sleep(1)

    def cleanup_rootfs(self):
        rootfs_path = os.path.join(PX4_DIR, "build/px4_sitl_default/rootfs")
        if os.path.exists(rootfs_path):
            subprocess.run(f"rm -rf {rootfs_path}", shell=True)
        time.sleep(1)

    def start_sitl_and_mavros(self):
        self.cleanup_procs()
        self.cleanup_rootfs()
        print("[Master] 正在启动仿真环境 (加速 + 无界面模式)...")
        speed_factor = 3
        # px4_cmd = f"cd {PX4_DIR} && PX4_SIM_SPEED_FACTOR={speed_factor} HEADLESS=1 make px4_sitl_default jmavsim"
        px4_cmd = f"cd {PX4_DIR} && make px4_sitl_default jmavsim"
        subprocess.Popen(["gnome-terminal", "--title=PX4", "--", "bash", "-c", f"{px4_cmd}; exec bash"])
        time.sleep(10)
        mavros_cmd = "ros2 launch mavros px4.launch fcu_url:=udp://:14540@"
        subprocess.Popen(["gnome-terminal", "--title=MAVROS", "--", "bash", "-c", f"{mavros_cmd}; exec bash"])
        time.sleep(15)
        self.env_running = True

    def minimize_crash_poc(self, crashing_params, crash_reason):
        """
        基于 Delta Debugging (贪心降维) 的最小崩溃组合提取算法
        """
        print("\n" + "!"*50)
        print("🔍 [PoC 提纯] 检测到崩溃，启动最小变异组合隔离算法...")        
        
        # 1. 找出所有相对于 cur 发生变异的参数
        mutated_keys = [k for k, v in crashing_params.items() if abs(float(v) - float(self.active_params[k]['cur'])) > 1e-4]
        test_params = crashing_params.copy()

        # === 利用已知黑名单进行免疫过滤 ===
        hit_known_bug = False
        # known_sig 是一个字典，如 {"A": 0.3, "B": 1.2}
        for known_sig in self.known_crash_signatures: 
            # 判断 known_sig 是否是当前突变集合的子集
            is_subset = True
            for k, v in known_sig.items():
                if k not in test_params or abs(float(test_params[k]) - float(v)) > 1e-4:
                    is_subset = False
                    break
            
            if is_subset:
                print(f"🕵️ 发现当前组合包含已知致毁子集: {known_sig}")
                # 剔除这个已知致毁子集（恢复为安全基准值）
                for k in known_sig.keys():
                    test_params[k] = float(self.active_params[k]['cur'])
                    if k in mutated_keys: mutated_keys.remove(k)
                hit_known_bug = True
                
        if hit_known_bug:
            print("🧪 正在测试剔除已知 Bug 后的剩余组合是否安全...")
            task = {"mode": self.target_phase, "params": test_params, "base_err": self.base_err, "base_vib": self.base_vib}
            _, crashed, success, _ = self.run_worker_task(task, is_calibration=True)
            
            if success and not crashed:
                print("✅ 剩余组合安全飞行！证明本次 Crash 纯粹由旧 Bug 触发，直接跳过重复提纯。")
                print("!"*50 + "\n")
                return 
            elif success and crashed:
                print("💥 剔除旧 Bug 后依然崩溃！发现了隐藏的新漏洞组合！继续提纯...")
                crashing_params = test_params.copy() # 用剔除旧病后的集合继续提纯
            else:
                return # 环境异常
        
        
        
        # 2. Delta Debugging 提纯流程
        minimal_mutations = mutated_keys.copy()
        for p_test in mutated_keys:
            if len(minimal_mutations) <= 1:
                break # 已经只剩一个变异参数了，这就是最小集
                
            print(f"  [降维测试] 尝试将 {p_test} 恢复为基准值...")
            test_params = crashing_params.copy()
            # 恢复该参数为基准值
            test_params[p_test] = float(self.active_params[p_test]['cur'])
            
            task = {
                "mode": self.target_phase,
                "params": test_params,
                "base_err": self.base_err,
                "base_vib": self.base_vib
            }
            
            # 以校准模式运行，不计入 Fuzzing 迭代次数
            _, crashed, success, _ = self.run_worker_task(task, is_calibration=True)
            
            if not success:
                print(f"  ⚠️ 环境异常，为防止漏报，保守保留参数 {p_test}。")
                continue

            if crashed:
                print(f"  ✅ 剔除 {p_test} 后仍然崩溃！说明它是无辜的(干扰项)，已将其移除。")
                minimal_mutations.remove(p_test)
                crashing_params = test_params.copy() # 更新当前的最小崩溃集合，用更简化的参数继续测
            else:
                print(f"  🛑 剔除 {p_test} 后无人机恢复安全！说明 {p_test} 是崩溃的【必要组成部分】，必须保留。")
                
            
        # 提取后立即将其加入黑名单
        minimal_combination = {k: crashing_params[k] for k in minimal_mutations}
        if minimal_combination not in self.known_crash_signatures:
            self.known_crash_signatures.append(minimal_combination)
        
        # 3. 记录提纯后的 PoC
        poc_record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "crash_reason": crash_reason,
            "original_count": len(mutated_keys),
            "minimal_count": len(minimal_mutations),
            "minimal_combination": minimal_combination,
            "ignored_noise_params": list(set(mutated_keys) - set(minimal_mutations))
        }
        
        pocs = []
        if os.path.exists(POC_FILE):
            try:
                with open(POC_FILE, 'r') as f: pocs = json.load(f)
            except: pass
            
        pocs.append(poc_record)
        with open(POC_FILE, 'w') as f: json.dump(pocs, f, indent=4)
        
        self.unique_crashes_found += 1
        print(f"🔥 提纯完成！致毁核心 ({len(minimal_mutations)}个参数) 已保存。")
        if len(minimal_mutations) > 1:
            print(f"🚀 捕获高价值交互漏洞！参数组合: {list(minimal_mutations)}")
        print("!"*50 + "\n")

    # 8. 调用 Worker 执行并返回 (score, crashed, success, metrics)
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
            proc.wait(timeout=300) # 5分钟超时保护
            self.total_physical_runs += 1

            if os.path.exists(WORKER_TEMP_FILE):
                with open(WORKER_TEMP_FILE, 'r') as f:
                    res = json.load(f)
                    
                status = res.get('status', '')
                if "ENV" in status or "TIMEOUT" in status or "EXCEPTION" in status:
                    self.env_running = False
                    return 0.0, False, False, {} # 环境崩了，任务无效
                    
                score = res.get('score', 0.0)
                crashed = res.get('crashed', False) or "CRASH" in status
                metrics = res.get('metrics', {})

                # 保存实验记录
                if not is_calibration and 'id' in res:
                    self.results.append(res)
                    with open(FUZZ_LOG_FILE, 'w') as f:
                        json.dump(self.results, f, indent=4)
                    
                if crashed: self.env_running = False # 物理炸机，下一轮必须重置仿真
                return score, crashed, True, metrics
            else:
                self.env_running = False
                return 0.0, False, False, {}
        except Exception as e:
            print(f"❌ 调度器异常: {e}")
            self.env_running = False
            return 0.0, False, False, {}
        
    def run_fuzz_loop(self):
        print(f"\n🚀 启动 C-UCB 与梯度引导星型 Fuzzing...")

        self.calibrate_system() # 启动前先校准

        base_params = {name: info['cur'] for name, info in self.active_params.items()}
        while True:
            print("\n" + "="*60)
            print("▶️ [轨迹锚定] 正在测量本轮物理基准分数...")
            base_task = {"mode": self.target_phase, "params": base_params.copy(), "base_err": self.base_err, "base_vib": self.base_vib}
            self.env_running = False # 强制每轮重置环境，确保每次都是从干净状态开始
            base_score, crashed, success, _ = self.run_worker_task(base_task)

            # --- 步骤 1：UCB 选择 3 个老虎机拉杆 ---
            trajectory_depth = random.randint(3, 8) # 随机组合深度 3 到 8 个参数
            selected_targets = self.get_c_ucb_targets(trajectory_depth)
            print(f"🎯 开启深度耦合轨迹 | 规划组合深度: {trajectory_depth} | 目标: {selected_targets}")
            
            # 累积状态字典：每走一步，上一步的变异都会被保留！
            accumulated_params = base_params.copy()
            current_node_score = base_score

            # --- 步骤 3：严格的星型发散变异 (避免参数组合污染) ---
            for step, p_name in enumerate(selected_targets):
                
                new_val, norm_delta, direction = self.mutate_single_param_smart(accumulated_params, p_name)
                
                if norm_delta < 1e-4:
                    print("⏩ 变量移动过小，跳过以防止除零误差。")
                    continue
                    
                # 更新轨迹状态
                accumulated_params[p_name] = new_val
                delta_params = {
                    k: v for k, v in accumulated_params.items() 
                    if v != base_params[k]
                }
                mutated_task = {
                    "mode": self.target_phase, 
                    "params": delta_params,
                    "base_err": self.base_err,
                    "base_vib": self.base_vib
                    }
                
                # 执行突变测试
                new_score, crashed, success, _ = self.run_worker_task(mutated_task)
                
                if not success:
                    print("⚠️ 环境异常，中断当前轨迹。")
                    break
                
                score_delta = new_score - current_node_score
                ee = abs(score_delta) / norm_delta
                total_delta_so_far = new_score - base_score

                if crashed:
                    # 炸机不干扰 EE 计算，只增加独立惩罚计数
                    print(f"💥 组合漏洞命中！(深度 {step+1}) 触发崩溃！")
                    self.stats[p_name]['crash_count'] += 1
                    self.minimize_crash_poc(accumulated_params, crash_reason="Fuzzing Triggered")
                    break # 星型拓扑被破坏，必须重启中心点
                else:
                    self.update_online_gsa(p_name, ee, score_delta, direction, total_delta_so_far)
                    current_node_score = new_score
                    print(f"📊 {p_name} | Step {step} | TotalDelta: {total_delta_so_far:.2f} | EE: {ee:.2f}")
                                    
            # 每一轮结束，更新并保存论文研究效率数据
            self.save_efficiency_report()

    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="Mission", choices=["Takeoff", "Mission", "Hold", "Landing"])
    args = parser.parse_args()
    
    master = FuzzMaster(target_phase=args.phase)
    try:
        master.run_fuzz_loop()
    except KeyboardInterrupt:
        print("\n🛑 安全退出，已保存敏感度数据。")
        master.save_final_report()
        master.cleanup_procs()