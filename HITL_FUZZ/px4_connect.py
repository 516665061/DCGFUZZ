#!/usr/bin/env python3
import os
import sys
import time
import argparse
import subprocess
import threading

import rclpy
from rclpy.node import Node
from mavros_msgs.msg import State

# --- 终端高亮配色配置 ---
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

class PX4ConnectionMonitor(Node):
    """
    一个简单的 ROS2 节点，专门用于监听 PX4 的连接状态
    """
    def __init__(self):
        super().__init__('px4_connection_monitor')
        # 订阅 MAVROS 的状态话题
        self.subscription = self.create_subscription(
            State,
            '/mavros/state',
            self.state_callback,
            10
        )
        self.is_connected = False
        self.mode = ""

    def state_callback(self, msg):
        self.is_connected = msg.connected
        self.mode = msg.mode
        # 如果连接成功，打印一次调试信息（可选）
        # self.get_logger().info(f'Current State: Connected={msg.connected}, Mode={msg.mode}')

def launch_mavros_in_new_terminal(fcu_url, gcs_url):
    """
    在新的终端窗口中启动 MAVROS
    """
    # 构造 ROS2 启动命令
    ros_cmd = (
        f"ros2 launch mavros px4.launch "
        f"fcu_url:={fcu_url} "
        f"gcs_url:={gcs_url} "
        # --- 关键修改开始 ---
        f"target_system_id:=1 "       # 明确目标无人机的 ID (PX4 默认为 1)
        f"target_component_id:=1 "    # 明确目标组件 ID
        f"system_id:=254 "      # <--- 将本机(MAVROS)伪装成 GCS (ID 254)
        f"component_id:=0 "   # MAVROS 默认组件 ID，也可以改为 190 (Mission Computer)
        # --- 关键修改结束 ---
        f"pluginlists_yaml:=/opt/ros/humble/share/mavros/launch/px4_pluginlists.yaml "
        f"config_yaml:=./px4_config.yaml"
    )

    print(f"{Colors.CYAN}[INFO] Launching MAVROS in a separate terminal...{Colors.ENDC}")
    
    # 检测系统终端并启动新窗口
    # 这里以 gnome-terminal 为主，如果需要 xterm 可以修改
    # -- 在新窗口执行命令后 exec bash 是为了让窗口不立即关闭，方便查看日志
    try:
        subprocess.Popen([
            "gnome-terminal", "--", "bash", "-c", 
            f"echo 'Starting MAVROS Connection...'; {ros_cmd}; exec bash"
        ])
    except FileNotFoundError:
        # Fallback 尝试 xterm
        try:
            subprocess.Popen([
                "xterm", "-e", f"{ros_cmd}; bash"
            ])
        except FileNotFoundError:
            print(f"{Colors.FAIL}[ERROR] No suitable terminal found (gnome-terminal or xterm).{Colors.ENDC}")
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="PX4 HITL/SITL Connection Bridge (Split Terminal)")
    
    # 连接方式参数
    parser.add_argument("--mode", choices=['udp', 'serial'], default='udp', help="Connection mode")
    
    # UDP 参数
    parser.add_argument("--ip", default="127.0.0.1", help="Target IP for UDP (usually localhost for bridge)")
    parser.add_argument("--udp-in", default="14540", help="UDP Bind Port (Local)")
    parser.add_argument("--udp-out", default="14557", help="UDP Target Port (Remote)")
    
    # 串口 参数
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial port for HITL hardware")
    parser.add_argument("--baud", default="57600", help="Baudrate for Serial")
    
    args = parser.parse_args()

    # 1. 构建 fcu_url
    if args.mode == 'udp':
        # 格式: udp://[bind_host][:bind_port]@[remote_host][:remote_port]
        # 注意: HITL连接时，如果是本机转发，通常也是 127.0.0.1
        fcu_url = f"udp://:{args.udp_in}@{args.ip}:{args.udp_out}"
        print(f"{Colors.HEADER}[CONF] Mode: UDP HITL/SITL{Colors.ENDC}")
    else:
        fcu_url = f"{args.port}:{args.baud}"
        print(f"{Colors.HEADER}[CONF] Mode: Serial HITL{Colors.ENDC}")

    gcs_url = "udp://@localhost:14550" # 转发给 QGC 或其他地面站
    
    print(f"{Colors.BLUE}[CONF] FCU URL: {fcu_url}{Colors.ENDC}")

    # 2. 初始化 ROS2
    rclpy.init()
    monitor_node = PX4ConnectionMonitor()

    # 3. 在新窗口启动 MAVROS
    launch_mavros_in_new_terminal(fcu_url, gcs_url)

    # 4. 在主线程中循环检测连接状态
    print(f"{Colors.WARNING}[WAIT] Waiting for Heartbeat from PX4... (Check the other terminal for logs){Colors.ENDC}")
    
    start_time = time.time()
    try:
        while rclpy.ok():
            # 处理一次 ROS 回调
            rclpy.spin_once(monitor_node, timeout_sec=0.1)

            if monitor_node.is_connected:
                # --- 连接成功 ---
                print("\n" + "="*50)
                print(f"{Colors.GREEN}{Colors.BOLD}   [SUCCESS] PX4 CONNECTED SUCCESSFULLY!   {Colors.ENDC}")
                print(f"{Colors.GREEN}   Mode: {monitor_node.mode} | Link: OK{Colors.ENDC}")
                print("="*50 + "\n")
                
                print(f"{Colors.HEADER}[*] Bridge is active. You can now start the FUZZER.{Colors.ENDC}")
                
                # 这里可以插入启动 Fuzzer 的代码，或者直接 break 退出循环让脚本挂起
                # 为了保持连接状态监听，我们通常选择让它继续运行，或者在这里返回
                # break 
                
                # 如果你想保持脚本运行以监控连接断开的情况：
                while monitor_node.is_connected:
                    rclpy.spin_once(monitor_node, timeout_sec=1.0)
                    if not monitor_node.is_connected:
                        print(f"{Colors.FAIL}[!] CONNECTION LOST!{Colors.ENDC}")
                        break
            
            # 超时处理 (例如 60秒还没连上)
            if time.time() - start_time > 300:
                print(f"{Colors.FAIL}[TIMEOUT] No Heartbeat received in 60 seconds.{Colors.ENDC}")
                break

    except KeyboardInterrupt:
        print(f"\n{Colors.BLUE}[EXIT] Stopping monitor script.{Colors.ENDC}")
    finally:
        monitor_node.destroy_node()
        rclpy.shutdown()
        print(f"{Colors.WARNING}[NOTE] Don't forget to close the MAVROS terminal window manually if needed.{Colors.ENDC}")

if __name__ == "__main__":
    main()