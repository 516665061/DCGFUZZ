import random
from copy import deepcopy
from typing import Dict, Any, List

class Mutator:
    def __init__(self, param_cfg: Dict[str, Any], strategy: str = "random_uniform"):
        """
        :param param_cfg: 参数配置字典，包含每个参数的 min, max, step 等信息
        :param strategy: 变异策略 'random_uniform' 或 'step'
        """
        self.param_cfg = param_cfg
        self.strategy = strategy
        self._validate_cfg()

    def _validate_cfg(self):
        """预检查配置合法性"""
        for name, rule in self.param_cfg.items():
            if "min" not in rule or "max" not in rule:
                raise ValueError(f"Parameter config for {name} must contain 'min' and 'max'")
            if rule["min"] > rule["max"]:
                raise ValueError(f"Invalid range for {name}: min({rule['min']}) > max({rule['max']})")

    def mutate(self, base_params: Dict[str, Any], k: int) -> Dict[str, Any]:
        """
        对给定的参数集进行变异
        :param base_params: 当前的基准参数字典
        :param k: 随机挑选多少个参数进行变异
        :return: 变异后的完整参数字典
        """
        child = deepcopy(base_params)
        all_keys = list(self.param_cfg.keys())
        
        # 确保选择数量不超过总数
        k = min(k, len(all_keys))
        picked = random.sample(all_keys, k)

        for name in picked:
            rule = self.param_cfg[name]
            # 强制类型转换以确保数值计算安全
            vmin = float(rule["min"])
            vmax = float(rule["max"])
            
            # 判断目标是否应该是整数（PX4中有许多INT32参数）
            # 优先检查配置中的 type 字段，如果没有则根据 min/max 的原始类型判断
            is_integer = rule.get("type") == "int" or (isinstance(rule["min"], int) and isinstance(rule["max"], int))

            if self.strategy == "random_uniform":
                if is_integer:
                    v = random.randint(int(vmin), int(vmax))
                else:
                    v = random.uniform(vmin, vmax)
            
            elif self.strategy == "step":
                step = float(rule.get("step", 1.0))
                if step <= 0:
                    v = random.uniform(vmin, vmax) # 回退策略
                else:
                    # 计算步数跨度
                    num_steps = int((vmax - vmin) / step)
                    if num_steps <= 0:
                        v = vmin if random.random() > 0.5 else vmax
                    else:
                        idx = random.randint(0, num_steps)
                        v = vmin + idx * step
                        # 确保不超过最大值
                        v = min(v, vmax)

                if is_integer:
                    v = int(round(v))
            
            else:
                # 默认回退到原始值
                v = base_params.get(name)

            child[name] = v

        return child

    def get_random_init(self) -> Dict[str, Any]:
        """生成一个全新的随机配置（可选，用于初始化种群）"""
        all_params = {}
        for name in self.param_cfg.keys():
            all_params[name] = self.param_cfg[name]["min"] # 或者用中间值
        return self.mutate(all_params, len(self.param_cfg))