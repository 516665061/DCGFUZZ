import subprocess
import json
import time
import os
import sys
import math
import argparse
import random
from pathlib import Path

# === 全局配置 ===
SEMANTIC_SPACE_FILE = "semantic_space.json"
SENSITIVITY_REPORT = "online_sensitivity.json"
WORKER_SCRIPT = "gsa_worker.py"
CHECKPOINT_FILE = "mcts_checkpoint.json"
CRASH_LOG_DIR = "./crashes_poc"

# MCTS 核心超参数
C_PUCT = 1.414  # 探索因子：控制开发（Exploitation）与探索（Exploration）的平衡

class MCTSNode:
    """MCTS 搜索树节点"""
    def __init__(self, param_name=None, value=None, parent=None, prior=0.1):
        self.param_name = param_name      # 该节点对应的变异参数名
        self.value = value                # 变异后的数值
        self.parent = parent
        self.children = []
        
        self.visits = 0                   # N(s,a): 访问次数
        self.total_reward = 0.0           # W(s,a): 累积回报（得分）
        self.prior = prior                # P(s,a): 先验概率（由 GSA 敏感度提供）
        self.is_crash = False             # 标记该路径是否导致崩溃

    @property
    def q_value(self):
        """Q(s,a): 状态动作价值（平均得分）"""
        return self.total_reward / self.visits if self.visits > 0 else 0

    def get_uct_score(self):
        """
        PUCT 公式实现：利用项 + 探索项
        Score = Q + C_puct * P * (sqrt(N_parent) / (1 + N_child))
        """
        if self.visits == 0:
            return float('inf') # 确保每个新生成的节点至少被访问一次
        
        # 计算探索增益 (随访问次数增加而衰减)
        parent_visits = self.parent.visits if self.parent else self.visits
        u_value = C_PUCT * self.prior * (math.sqrt(parent_visits) / (1 + self.visits))
        
        return self.q_value + u_value

class SGMCTSMaster:
    def __init__(self, target_phase, max_depth=3):
        self.target_phase = target_phase
        self.max_depth = max_depth # 限制变异叠加的深度
        self.global_id = 0
        
        # 1. 加载重构后的语义空间
        self.semantic_data = self.load_json(SEMANTIC_SPACE_FILE)[target_phase]
        self.params_pool = self.semantic_data["parameters"]
        
        # 2. 加载敏感度先验 (GSA 结果)
        self.sensitivity_map = self.load_json(SENSITIVITY_REPORT, default={})
        
        # 3. 初始化或恢复搜索树
        self.root = self.load_checkpoint() or MCTSNode(prior=1.0)
        
        if not os.path.exists(CRASH_LOG_DIR):
            os.makedirs(CRASH_LOG_DIR)

    def load_json(self, path, default=None):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default

    def select(self):
        """
        Selection 相位：从根节点出发，依据 PUCT 分数向下挖掘
        """
        curr = self.root
        depth = 0
        while curr.children and depth < self.max_depth:
            # 如果该节点下还有参数未被尝试，则停止 Selection 进入 Expansion
            if len(curr.children) < len(self.params_pool):
                break
            curr = max(curr.children, key=lambda n: n.get_uct_score())
            depth += 1
        return curr, depth

    def expand(self, node, current_depth):
        """
        Expansion 相位：根据敏感度引导，展开一个新的变异分支
        """
        if current_depth >= self.max_depth:
            return node # 达到最大深度，直接开始仿真
            
        tried_params = [child.param_name for child in node.children]
        untried_params = [p for p in self.params_pool.keys() if p not in tried_params]
        
        if not untried_params:
            return node
            
        # 启发式选择：在未尝试参数中，选择敏感度最高的
        target_p = max(untried_params, key=lambda p: self.sensitivity_map.get(p, 0.01))
        
        # 计算变异值 (调用语义感知变异)
        new_val = self.mutate_param(target_p)
        
        # 创建新节点，注入先验概率 (GSA 敏感度越大，Prior 越高)
        prior = self.sensitivity_map.get(target_p, 0.1)
        new_node = MCTSNode(param_name=target_p, value=new_val, parent=node, prior=prior)
        node.children.append(new_node)
        
        return new_node

    def mutate_param(self, p_name):
        """基于语义空间的智能变异"""
        info = self.params_pool[p_name]
        
        # 如果是枚举，直接从合法值库中选
        if info.get("is_enum") and info.get("values"):
            v_list = list(info["values"].keys())
            return float(random.choice(v_list))
        
        # 如果是连续值，在 [min, max] 范围内进行均匀采样（可根据需要改为正态分布采样）
        new_v = random.uniform(info["min"], info["max"])
        return round(new_v, info.get("decimal", 4) or 4)

    def run_simulation(self, node):
        """
        Simulation 相位：执行仿真。
        严谨性体现：必须回溯路径，应用从根到叶子的所有变异！
        """
        self.global_id += 1
        
        # 路径回溯获取组合参数
        mutation_chain = {}
        curr = node
        while curr.parent:
            mutation_chain[curr.param_name] = curr.value
            curr = curr.parent
            
        # 构造任务报文 (对接 gsa_worker.py)
        task = {
            "id": self.global_id,
            "mode": self.target_phase,
            "params": mutation_chain,
            "base_err": 0.5, 
            "base_vib": 0.05
        }
        
        temp_res_file = f".mcts_res_{self.global_id}.json"
        
        print(f"🛠  仿真任务 {self.global_id} | 变异组合数: {len(mutation_chain)}")
        for p, v in mutation_chain.items():
            print(f"   - {p}: {v}")

        try:
            # 调用 worker 脚本，带超时保护
            cmd = [sys.executable, WORKER_SCRIPT, json.dumps(task), temp_res_file]
            subprocess.run(cmd, check=True, timeout=450) 
            
            if os.path.exists(temp_res_file):
                with open(temp_res_file, 'r') as f:
                    res = json.load(f)
                os.remove(temp_res_file)
                
                score = res.get("score", 0.0)
                if res.get("crashed", False):
                    print(f"🔥 [CRASHED] 发现导致崩溃的参数路径！")
                    self.save_crash_poc(task, res)
                    node.is_crash = True
                    score += 200.0 # 给予极高的 Reward 引导树向该区域生长
                
                return score
        except Exception as e:
            print(f"⚠️ 仿真异常失败: {e}")
            return 0.0
        return 0.0

    def backpropagate(self, node, reward):
        """
        Backpropagation 相位：更新路径上所有节点的 visits 和 reward
        """
        curr = node
        while curr:
            curr.visits += 1
            curr.total_reward += reward
            curr = curr.parent

    def save_crash_poc(self, task, res):
        """保存崩溃现场 PoC"""
        ts = int(time.time())
        filename = f"mcts_crash_id{self.global_id}_{ts}.json"
        with open(os.path.join(CRASH_LOG_DIR, filename), 'w') as f:
            json.dump({"task": task, "result": res}, f, indent=4)

    def save_checkpoint(self):
        """序列化整个搜索树"""
        def node_to_dict(n):
            return {
                "p": n.param_name, "v": n.value,
                "n": n.visits, "w": n.total_reward,
                "prior": n.prior, "crash": n.is_crash,
                "c": [node_to_dict(child) for child in n.children]
            }
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(node_to_dict(self.root), f)

    def load_checkpoint(self):
        """从文件恢复搜索树"""
        if not os.path.exists(CHECKPOINT_FILE): return None
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                data = json.load(f)
            def dict_to_node(d, parent=None):
                n = MCTSNode(d['p'], d['v'], parent, d['prior'])
                n.visits, n.total_reward, n.is_crash = d['n'], d['w'], d['crash']
                n.children = [dict_to_node(c, n) for c in d['c']]
                return n
            print("💾 成功加载历史搜索树进度。")
            return dict_to_node(data)
        except: return None

    def main_loop(self, iterations=1000):
        print(f"🚀 SG-MCTS Fuzzer 启动 | 相位: {self.target_phase} | 最大组合深度: {self.max_depth}")
        try:
            for i in range(iterations):
                # 1. 选择 & 2. 扩展
                leaf, depth = self.select()
                target_node = self.expand(leaf, depth)
                
                # 3. 仿真
                reward = self.run_simulation(target_node)
                
                # 4. 回溯更新
                self.backpropagate(target_node, reward)
                
                if i % 10 == 0:
                    self.save_checkpoint()
                    print(f"📊 进度: {i}/{iterations} | 树节点总数: {self.count_nodes(self.root)}")
        except KeyboardInterrupt:
            self.save_checkpoint()
            print("\n🛑 已安全停止并保存进度。")

    def count_nodes(self, node):
        return 1 + sum(self.count_nodes(c) for c in node.children)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="Mission", choices=["Takeoff", "Mission", "Hold", "Landing"])
    parser.add_argument("--iters", type=int, default=1000)
    args = parser.parse_args()
    
    master = SGMCTSMaster(target_phase=args.phase)
    master.main_loop(iterations=args.iters)