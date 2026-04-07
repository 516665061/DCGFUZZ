import os
import json
import numpy as np

class SGFFMemory:
    def __init__(self, active_params_dict: dict, stagnation_limit: int = 5):
        """
        初始化 SGFF 记忆库与空间边界
        :param active_params_dict: 从 semantic_space.json 解析出的激活参数字典
        :param stagnation_limit: 允许局部停滞的最大迭代次数，超过则触发白洞排斥
        """
        self.active_params_dict = active_params_dict
        self.param_names = list(active_params_dict.keys())
        self.dim = len(self.param_names)
        
        # 构建物理边界向量 (供 Numpy 数学引擎使用)
        self.bounds_min = np.zeros(self.dim)
        self.bounds_max = np.zeros(self.dim)
        
        for i, name in enumerate(self.param_names):
            p_info = active_params_dict[name]
            self.bounds_min[i] = float(p_info['min'])
            self.bounds_max[i] = float(p_info['max'])
            
        # 核心记忆体：存放 (x_vector, score)
        self.memory_bank = []
        
        # 停滞与逃逸控制
        self.stagnation_limit = stagnation_limit
        self.stagnation_counter = 0
        self.global_max_score = 0.0
        
        # 极性反转惩罚值 (当变成白洞时，赋予的强大排斥力)
        self.repulsion_penalty = -50.0 

    def dict_to_vector(self, param_dict: dict) -> np.ndarray:
        """将飞控的参数字典翻译为 Numpy 坐标向量"""
        vec = np.zeros(self.dim)
        for i, name in enumerate(self.param_names):
            vec[i] = float(param_dict.get(name, 0.0))
        return vec

    def vector_to_dict(self, vector: np.ndarray, original_dict: dict) -> dict:
        """将计算出的 Numpy 向量翻译回飞控字典，并恢复数据精度与枚举约束"""
        new_dict = {}
        for i, name in enumerate(self.param_names):
            val = vector[i]
            p_info = original_dict[name]
            
            # 1. 处理枚举类型 (Enum Constraints)
            if p_info.get('is_enum') and p_info.get('values'):
                # 提取该参数所有允许的合法整数值
                valid_values = [int(v['value']) for v in p_info['values']]
                # 找到离当前连续向量 val 最近的合法枚举值 (吸附)
                closest_val = min(valid_values, key=lambda x: abs(x - val))
                new_dict[name] = closest_val
                
            # 2. 处理普通整数 (Int32)
            elif p_info.get('type') == 'Int32':
                new_dict[name] = int(round(val))
                
            # 3. 处理浮点数 (Float)
            else:
                decimal = p_info.get('decimal', 4)
                new_dict[name] = round(val, decimal) if decimal is not None else round(val, 4)
                
        return new_dict

    def add_record(self, param_dict: dict, score: float):
        """
        将新的飞行测试结果压入记忆库，并更新停滞状态
        """
        x_vec = self.dict_to_vector(param_dict)
        self.memory_bank.append((x_vec, score))
        
        # 更新停滞计数器
        if score > self.global_max_score:
            self.global_max_score = score
            self.stagnation_counter = 0 # 发现更高分，重置计数器
            print(f"🌟 [SGFF Memory] 突破历史最高分: {score:.2f}！引力场加深。")
        else:
            self.stagnation_counter += 1

    def check_and_apply_repulsion(self) -> bool:
        """
        [算法核心 4] 禁忌斥力逃逸 (极性反转)
        检查是否陷入局部最优，如果是，将最近的高分黑洞反转为白洞
        :return: 是否触发了排斥
        """
        if self.stagnation_counter >= self.stagnation_limit and len(self.memory_bank) > 0:
            print(f"⚠️ [SGFF Memory] 连续 {self.stagnation_counter} 次停滞！触发禁忌斥力逃逸 (Tabu Repulsion)！")
            
            # 只在最近的 N 步历史中寻找那个困住粒子的"局部黑洞"
            lookback_steps = min(self.stagnation_limit * 3, len(self.memory_bank))
            recent_memory = self.memory_bank[-lookback_steps:]

            # 找到当前记忆库中得分最高的那个黑洞 (且还没被反转过的)
            # 为了防止直接改写已有的元组，我们需要重建 memory_bank
            max_idx = -1
            local_max = -float('inf')
            
            for i, (x_vec, score) in enumerate(recent_memory):
                if score > local_max and score > 0: 
                    local_max = score
                    max_idx = i
                    
            if max_idx != -1:
                # 极性反转：将这个高分诱饵变成巨大的负分斥力源
                real_idx = len(self.memory_bank) - lookback_steps + max_idx
                x_vec, old_score = self.memory_bank[real_idx]
                self.memory_bank[real_idx] = (x_vec, self.repulsion_penalty)
                
                print(f"💥 [SGFF Memory] 已将困住粒子的局部峰值 ({old_score:.2f}) 反转为白洞，强制弹射！")
                self.stagnation_counter = 0
                return True
        return False

    def blacklist_poc_crash(self, param_dict: dict):
        """
        当触发真实 Crash 并记录 PoC 后，调用此方法将其直接炸成白洞，防止反复踩坑
        """
        x_vec = self.dict_to_vector(param_dict)
        # 压入一个极其夸张的负分，让 Fuzzer 以后绝对不敢靠近这里
        self.memory_bank.append((x_vec, self.repulsion_penalty * 2))
        self.stagnation_counter = 0
        self.global_max_score = 0.0
        print("🛡️ [SGFF Memory] 已将致毁 PoC 坐标永久禁忌化。")

    # === 新增：断点续传支持 ===
    def save_log(self, filepath):
        """持久化落盘，保存为供人类和程序可读的 JSON"""
        log_data = []
        for x_vec, score in self.memory_bank:
            param_dict = self.vector_to_dict(x_vec, self.active_params_dict)
            log_data.append({"params": param_dict, "score": score})
        with open(filepath, 'w') as f:
            json.dump(log_data, f, indent=4)

    def load_log(self, filepath):
        """读取历史记录，瞬间重建引力场"""
        if os.path.exists(filepath):
            try: # 👈 加上容错护甲
                with open(filepath, 'r') as f:
                    log_data = json.load(f)
                for item in log_data:
                    self.add_record(item["params"], item["score"])
                print(f"🔄 [SGFF Memory] 成功从日志恢复 {len(log_data)} 个探索点，引力场重建完毕！")
                return True
            except Exception as e:
                print(f"⚠️ [SGFF Memory] 历史日志文件损坏 ({e})，已放弃读取，将开启全新宇宙。")
                return False
        return False