# oracle.py
"""
HITL/SITL Oracle: 负责物理状态判定与异常检测逻辑。
"""
import time
import math
import logging

class HITLOracle:
    def __init__(self, px4_interface, config: dict = None, target_waypoints=None, log_filename="mission_trace.log"):
        self.px4 = px4_interface
        self.target_waypoints = target_waypoints or []

        self.logger = logging.getLogger("OracleLogger")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            # 文件输出
            fh = logging.FileHandler(log_filename)
            fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            self.logger.addHandler(fh)
            
            # 控制台输出 (保留 print 的效果)
            ch = logging.StreamHandler()
            self.logger.addHandler(ch)
        

        # 判定阈值配置
        self.cfg = {
            "ground_z": 0.15,               # 地面高度阈值 (m)
            "hard_descent_vz": -4.0,        # 极速坠落阈值 (m/s)
            "instability_vz": -1.2,         # 悬停不稳阈值 (m/s)
            "max_tilt_deg": 60.0,           # 最大倾斜角阈值 (deg)
            "deadlock_timeout": 5.0,        # 数据链路超时阈值 (s)
            "max_vel_xy": 15.0,             # 水平最大逃逸速度阈值 (m/s)
            "route_dev_max": 5.0,           # 最大允许偏离航线距离 (m)
            "oscillation_var_limit": 5.0,   # 剧烈震荡”的方差阈值
            "error_keywords": ["fail", "error", "emergency", "diverg", "reject","preflight", "arming denied", "ekf", "timeout"]
        }
        if config:
            self.cfg.update(config)

        self.last_data_time = time.time()

    def check_health(self):
        """
        核心判定函数：返回 (is_crashed, reason)
        被 Fuzzer 的监控线程高频调用。
        """
        data = self.px4.get_data() # 获取线程安全的快照
        now = time.time()
        
        # 1. 检查数据链路死锁 (Deadlock)
        # 如果长时间没有收到遥测回调更新，判定为固件死锁或链路断开
        if now - data['timestamp'] > self.cfg['deadlock_timeout']:
            return True, f"Telemetry Deadlock: no update for {now - data['timestamp']:.1f}s"

        # 2. 系统严重日志与起飞拒绝
        # 在判断是否武装(armed)之前，先检查是否有阻止起飞的严重报错
        status_text = data.get('status_text', "")
        severity = data.get('severity', 99)
        
        if severity <= 3 and len(status_text) > 0:
            return True, f"System Error (Sev={severity}): {status_text}"
            
        text_lower = status_text.lower()
        for keyword in self.cfg['error_keywords']:
            if keyword in text_lower:
                return True, f"Critical Log Found: {status_text}"
            
        # 提取基础数据
        try:
            pose = data['pose'].pose
            pos = pose.position
            orient = pose.orientation
            vel = data['velocity'].twist
            state = data['state']
            
            # 如果没解锁，通常不判定为 Crash (除非是由于参数导致无法解锁，但这属于另一种 Failure)
            if not state.armed:
                return False, None
                
        except (AttributeError, KeyError):
            return False, None # 数据尚未就绪

        # 3. 姿态判定 (Attitude Check)
        # 将四元数转换为 Roll/Pitch (度)
        roll, pitch, _ = self._quat_to_euler(orient.x, orient.y, orient.z, orient.w)
        max_tilt = max(abs(roll), abs(pitch))
        
        if max_tilt > self.cfg['max_tilt_deg']:
            return True, f"Attitude Flip (CRASH): {max_tilt:.1f}° exceeds limit"

        # 4. 动力学判定 (Dynamics Check)
        is_crash = False
        reason = None

        vz = vel.linear.z
        curr_z = pos.z
        # 判定极速坠落
        if vz < self.cfg['hard_descent_vz']:
            return True, f"Hard Descent (CRASH): vz={vz:.2f}m/s"
        
        # 判定异常触地 (Offboard/Mission 模式下，高度低且仍在向下加速或有较大向下速度)
        # 增加 state.mode 判断，避免把正常的降落误判为坠毁
        if curr_z < self.cfg['ground_z']:
            # 如果不是在降落模式，且接触了地面
            if state.mode not in ["AUTO.LAND", "AUTO.RTL"] and vz < -0.2:
                return True, f"Ground Collision (CRASH) in {state.mode}: z={curr_z:.2f}m"
        
        # 5. Route Deviation Oracle (航线偏离)
        # 判定 A: 水平速度过大（可能的飞逃）
        vx, vy = vel.linear.x, vel.linear.y
        if (vx**2 + vy**2) > self.cfg['max_vel_xy']**2:
            return True, f"Route Deviation: Horizontal Flyaway (v={math.sqrt(vx**2+vy**2):.2f}m/s)"

        # 判定 B: 空间距离偏差
        # 如果传入了 target_waypoints，计算当前位置与预定轨迹的偏差
        if self.target_waypoints and hasattr(self, 'current_wp_index'):
            if self.current_wp_index < len(self.target_waypoints):
                target = self.target_waypoints[self.current_wp_index]
                dx = pos.x - target.get('x', pos.x)
                dy = pos.y - target.get('y', pos.y)
                dist = math.sqrt(dx**2 + dy**2) # 主要监控水平面偏航
                
                # 初始化距离追踪
                if not hasattr(self, 'last_wp_dist') or self.last_wp_dist is None:
                    self.last_wp_dist = dist
                    self.deviate_count = 0
                else:
                    # 如果距离目标越来越远 (容忍 0.3m 的控制超调或刹车滑行惯性)
                    if dist - self.last_wp_dist > 0.3:
                        self.deviate_count += 1
                    elif dist - self.last_wp_dist < -0.1: 
                        # 正在靠近目标，清除危险计数
                        self.deviate_count = max(0, self.deviate_count - 1)
                        
                    self.last_wp_dist = dist
                    
                    # 只有连续多次背向飞行，并且总距离超出阈值，才确认是真偏航
                    if self.deviate_count > 6 and dist > self.cfg['route_dev_max']:
                        return True, f"Route Deviation: Flyaway trend (dist={dist:.2f}m)"
            
        return False, None

    def _calculate_route_deviation(self, current_pos):
        """
        计算当前位置与目标航线的交叉轨迹误差 (Cross-Track Error)。
        完全依赖飞控的 mission/reached 进行状态同步。
        """
        if not self.target_waypoints:
            return 0.0
        
        # 1. 绝对精准的航点同步：只听飞控的！
        px4_reached = self.px4.current_wp_reached
        if px4_reached != -1:
            # 飞控说到达了 0，我们下一个目标就是 1
            self.current_wp_index = px4_reached + 1

        if self.current_wp_index >= len(self.target_waypoints):
            target = self.target_waypoints[-1]
            prev_target = self.target_waypoints[-2] if len(self.target_waypoints) > 1 else target
        else:
            target = self.target_waypoints[self.current_wp_index]
            prev_target = self.target_waypoints[max(0, self.current_wp_index - 1)]

        # 2. 科学计算跟踪误差 (点到直线的垂线距离)
        # 如果是起点(WP0)，还没形成航线，误差就是与起点的距离
        if self.current_wp_index == 0:
            dx = current_pos.x - target.get('x', current_pos.x)
            dy = current_pos.y - target.get('y', current_pos.y)
            dz = current_pos.z - target.get('z', current_pos.z)
            return math.sqrt(dx**2 + dy**2 + dz**2)
            
        # 计算无人机当前位置 P 到直线 AB (prev->target) 的水平偏航距离
        A_x, A_y = prev_target.get('x', 0.0), prev_target.get('y', 0.0)
        B_x, B_y = target.get('x', 0.0), target.get('y', 0.0)
        P_x, P_y = current_pos.x, current_pos.y
        
        AB_x = B_x - A_x
        AB_y = B_y - A_y
        
        segment_len = math.sqrt(AB_x**2 + AB_y**2)
        
        if segment_len == 0:
            cross_track_err = math.sqrt((P_x - A_x)**2 + (P_y - A_y)**2)
        else:
            # 叉乘求高 (即真实水平偏航误差)
            cross_track_err = abs((P_x - A_x) * AB_y - (P_y - A_y) * AB_x) / segment_len
            
        # 加上高度方向的误差
        z_err = abs(current_pos.z - target.get('z', current_pos.z))
        total_err = math.sqrt(cross_track_err**2 + z_err**2)
        
        # log_msg = (f"目标线 WP{self.current_wp_index-1}->WP{self.current_wp_index} | "
        #            f"pos({current_pos.x:.2f}, {current_pos.y:.2f}, {current_pos.z:.2f}) | "
        #            f"target({target.get('x'):.2f}, {target.get('y'):.2f}, {target.get('z'):.2f}) | "
        #            f"误差: {total_err:.2f}m")
        
        # # 这一行会自动同时保存到文件并打印到屏幕
        # self.logger.info(log_msg)
        
        return total_err

    def _quat_to_euler(self, x, y, z, w):
        """
        简单的四元数转欧拉角 (Roll, Pitch, Yaw) 单位：度
        """
        # roll (x-axis rotation)
        sinr_cosp = 2 * (w * x + y * z)
        cosr_cosp = 1 - 2 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp)

        # pitch (y-axis rotation)
        sinp = 2 * (w * y - z * x)
        if abs(sinp) >= 1:
            pitch = math.copysign(math.pi / 2, sinp) # use 90 degrees if out of range
        else:
            pitch = math.asin(sinp)

        # yaw (z-axis rotation)
        siny_cosp = 2 * (w * z + x * y)
        cosy_cosp = 1 - 2 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)
