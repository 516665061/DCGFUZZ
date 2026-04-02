#!/usr/bin/env python3
import argparse
import sys
import rclpy
import traceback
from fuzzer import PX4Fuzzer

def parse_args():
    parser = argparse.ArgumentParser(description="PX4 Parameter Fuzzer for HITL/SITL (ROS 2)")

    # --- 核心配置 ---
    parser.add_argument("--config", "-c", required=True, help="参数配置文件路径 (.json)")
    parser.add_argument("--outdir", default="fuzz_out", help="日志和 Crash Case 输出目录")
    
    # --- Fuzz 流程控制 ---
    parser.add_argument("--batch", type=int, default=1, help="执行多少个 Batch (Fuzz Run)")
    parser.add_argument("--iterations-per-run", type=int, default=50, help="每个 Batch 执行多少次迭代")
    parser.add_argument("--k", type=int, default=3, help="每次迭代变异的参数数量")
    parser.add_argument("--strategy", choices=['random_uniform', 'step'], default='random_uniform', 
                        help="变异策略: random_uniform (推荐) 或 step")
    parser.add_argument("--seed", type=int, default=42, help="随机种子，用于复现实验")

    # --- 时间控制 ---
    parser.add_argument("--eval-time", type=float, default=5.0, help="写入参数后，观察/评估是否 Crash 的时间窗口 (秒)")
    parser.add_argument("--set-delay", type=float, default=0.1, help="连续设置参数之间的间隔 (秒)")
    parser.add_argument("--loop-delay", type=float, default=0.3, help="迭代之间的缓冲时间 (秒)")

    # --- 自动化与安全 ---
    parser.add_argument("--auto", default="Hold", help="悬停还是任务")
    parser.add_argument("--takeoff-alt", type=float, default=2.5, help="自动起飞的目标高度 (米)")
    parser.add_argument("--continue-on-crash", action='store_true', help="Crash 后尝试恢复并继续 Fuzz (否则直接停止)")
    parser.add_argument("--restore", action='store_true', help="结束或 Crash 后恢复 Baseline 参数")

    return parser.parse_args()

def main():
    args = parse_args()
    
    # 初始化 ROS 2 上下文
    rclpy.init()
    
    node = None
    try:
        node = PX4Fuzzer(args)
        # 运行 Fuzzer 主逻辑 (这是一个阻塞调用，直到完成所有 batch 或发生 crash)
        node.run()
        
    except KeyboardInterrupt:
        print("\n[Main] 用户中断 (Ctrl+C)，正在停止...")
    except Exception as e:
        print(f"\n[Main] 发生未捕获异常: {e}")
        traceback.print_exc()
    finally:
        # 清理资源
        if node:
            print("[Main] 销毁节点资源...")
            # 确保销毁节点，这会触发 Fuzzer 和 Interface 中后台线程的清理
            node.destroy_node()
        
        if rclpy.ok():
            rclpy.shutdown()
        print("[Main] 程序已退出。")

if __name__ == "__main__":
    main()