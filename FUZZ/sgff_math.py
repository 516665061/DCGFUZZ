import numpy as np

class SGFFMathEngine:
    """
    高维敏感度引力场模糊测试 (SGFF) 核心数学引擎
    纯函数实现，不保存任何外部业务状态
    """
    
    @staticmethod
    def calculate_gravity_gradient(current_x: np.ndarray, memory_bank: list, sigma: float, ranges: np.ndarray) -> np.ndarray:
        """
        [算法核心 1] 计算 N 维引力势能场的梯度向量
        :param current_x: 当前粒子的参数坐标 (N维向量)
        :param memory_bank: 记忆库，元素为 (坐标向量x_i, 得分S_i)
        :param sigma: 引力扩散半径
        :param ranges: 参数的物理范围向量
        :return: N维梯度向量 (合力)
        """
        N = len(current_x)
        gradient = np.zeros(N)
        
        if not memory_bank:
            return gradient
            
        for x_i, score_i in memory_bank:
            # 1. 归一化距离差：让所有参数都在 [0, 1] 的同等尺度下计算空间距离
            norm_diff = (current_x - x_i) / (ranges + 1e-9)
            # 1. 计算欧氏距离的平方 ||x - x_i||^2
            dist_sq = np.sum(norm_diff ** 2)
            
            # 2. 计算高斯核权重 exp(-d^2 / 2*sigma^2)
            kernel_weight = np.exp(-dist_sq / (2.0 * sigma ** 2))
            
            # 3. 计算各个维度的偏导数并累加
            # 公式: S_i * exp(...) * (-(x - x_i) / sigma^2)
            force_contribution = score_i * kernel_weight * (-norm_diff / (sigma ** 2))
            gradient += force_contribution
            
        return gradient

    @staticmethod
    def adaptive_top_k_mask(gradient: np.ndarray, current_score: float, base_score: float, max_dim: int) -> list:
        """
        [算法核心 2] 自适应维度掩码 (决定变异谁，变异多少个)
        :param gradient: 计算出的 N 维梯度向量
        :param current_score: 粒子当前的系统不稳定得分
        :param base_score: 环境底噪基准分
        :param max_dim: 允许同时变异的最大参数数量
        :return: 选中的参数索引列表
        """
        # 取梯度的绝对值，评估各个参数的“破坏潜力”
        magnitude = np.abs(gradient)
        
        # 动态决定 K 值
        # 假设：如果当前得分远高于基准分(濒临崩溃)，我们减小 K 以精准打击；否则增大 K 进行广域探索
        danger_ratio = current_score / (base_score + 1e-5)
        
        if danger_ratio > 3.0:
            k = max(1, max_dim // 3)  # 危险期：收缩变异维度，防死锁
        elif danger_ratio > 1.5:
            k = max(2, max_dim // 2)  # 震荡期：中等规模变异
        else:
            k = max_dim               # 平稳期：放开手脚全量探索
            
        # 找出破坏潜力最大的前 K 个维度的索引
        # np.argsort 返回从小到大的索引，取最后 k 个即为最大的 k 个
        top_k_indices = np.argsort(magnitude)[-k:]
        
        return top_k_indices.tolist()

    @staticmethod
    def gravitational_step_update(current_x: np.ndarray, gradient: np.ndarray, 
                                  top_k_indices: list, bounds_min: np.ndarray, 
                                  bounds_max: np.ndarray, eta: float) -> np.ndarray:
        """
        [算法核心 3] 边界约束的引力步长更新 (计算最终注入飞控的数值)
        :param current_x: 当前参数坐标
        :param gradient: 梯度向量
        :param top_k_indices: 激活的参数索引
        :param bounds_min: 参数的物理下限
        :param bounds_max: 参数的物理上限
        :param eta: 基础学习率 (滑动步长系数)
        :return: 下一步的新参数坐标
        """
        next_x = np.copy(current_x)
        ranges = bounds_max - bounds_min
        
        # 提取被激活的梯度分量，计算局部范数用于归一化
        active_grad = np.zeros_like(gradient)
        for idx in top_k_indices:
            active_grad[idx] = gradient[idx]
            
        grad_norm = np.linalg.norm(active_grad)
        if grad_norm < 1e-9:
            return next_x # 梯度为 0，不移动
            
        # 归一化方向向量
        direction = active_grad / grad_norm
        
        # 仅更新被选中的维度
        for idx in top_k_indices:
            # 步长 = 学习率 * 方向分量 * 参数自身的物理极差
            step = eta * direction[idx] * ranges[idx]
            
            # 施加变异
            new_val = current_x[idx] + step
            
            # Clip 边界保护，防止超出飞控限制
            next_x[idx] = np.clip(new_val, bounds_min[idx], bounds_max[idx])
            
        return next_x