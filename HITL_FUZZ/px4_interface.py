# px4_interface.py
import time
import threading
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

# MAVROS 消息与服务
from geometry_msgs.msg import PoseStamped
from geometry_msgs.msg import TwistStamped
from mavros_msgs.msg import State, ExtendedState, StatusText, WaypointReached
from mavros_msgs.srv import CommandBool, SetMode, ParamSetV2
from rcl_interfaces.srv import GetParameters
from rcl_interfaces.msg import ParameterType
from sensor_msgs.msg import NavSatFix

class PX4Interface:
    def __init__(self, node: Node):
        self.node = node
        
        # 1. 数据存储与线程锁
        self._data_lock = threading.RLock()
        self.state = State()
        self.pose = PoseStamped()
        self.vel = TwistStamped()
        self.ext_state = ExtendedState()
        self.last_status_text = ""
        self.last_status_severity = 0
        self.global_pos = None
        self.last_update_time = time.time()
        self.current_wp_reached = -1 # 记录最后一次打卡的航点索引

        # 2. QoS 配置
        self.sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=5
        )

        # 3. 订阅器
        self.node.create_subscription(State, "/mavros/state", self.cb_state, 10)
        self.node.create_subscription(PoseStamped, "/mavros/local_position/pose", self.cb_pose, self.sensor_qos)
        self.node.create_subscription(TwistStamped, "/mavros/local_position/velocity_local", self.cb_vel, self.sensor_qos)
        self.node.create_subscription(ExtendedState, "/mavros/extended_state", self.cb_ext, self.sensor_qos)
        self.node.create_subscription(StatusText, "/mavros/statustext/recv", self.cb_statustext, self.sensor_qos)
        self.node.create_subscription(NavSatFix, "/mavros/global_position/global", self.cb_global_pos, self.sensor_qos)
        self.node.create_subscription(WaypointReached,"/mavros/mission/reached",self.cb_wp_reached,10)
        
        # 4. 服务客户端
        self.s_get_param = node.create_client(GetParameters, "/mavros/param/get_parameters")
        self.s_set_param = node.create_client(ParamSetV2, "/mavros/param/set")
        self.s_arm = node.create_client(CommandBool, "/mavros/cmd/arming")
        self.s_mode = node.create_client(SetMode, "/mavros/set_mode")

        # 5. 启动后台 Spin 线程
        self._stop_spin = threading.Event()
        self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._spin_thread.start()

    def _spin_loop(self):
        """后台执行 ROS 2 回调更新数据"""
        while rclpy.ok() and not self._stop_spin.is_set():
            rclpy.spin_once(self.node, timeout_sec=0.1)


    def stop(self):
        self._stop_spin.set()
        self._spin_thread.join(timeout=1.0)

    # --- 辅助方法：安全等待 Future 结果 ---
    def _wait_for_future(self, future, timeout_sec=35.0):
        """
        由于后台有 spin 线程，我们只需在此循环检查 Future 是否完成。
        """
        start_t = time.time()
        while not future.done():
            if time.time() - start_t > timeout_sec:
                return None # 超时返回 None
            #time.sleep(0.02) # 微小休眠避免占用 CPU
        try:
            return future.result()
        except Exception as e:
            self.node.get_logger().error(f"Future execution error: {e}")
            return None

    # --- 回调函数 ---
    def cb_state(self, msg):
        with self._data_lock:
            self.state = msg
            self.last_update_time = time.time()

    def cb_pose(self, msg):
        with self._data_lock:
            self.pose = msg
            self.last_update_time = time.time()

    def cb_vel(self, msg):
        with self._data_lock:
            self.vel = msg
            self.last_update_time = time.time()

    def cb_ext(self, msg):
        with self._data_lock:
            self.ext_state = msg
            self.last_update_time = time.time()
    
    def cb_statustext(self, msg):
        """记录 PX4 发出的文本日志"""
        with self._data_lock:
            self.last_status_text = msg.text
            self.last_status_severity = msg.severity
            # 严重等级: 0=EMERGENCY, 1=ALERT, 2=CRITICAL, 3=ERROR, 4=WARNING ...
            # 我们可以只记录 WARNING (4) 以上的级别
            if msg.severity <= 3: 
                self.node.get_logger().error(f"[PX4 LOG] {msg.text}")
    def cb_global_pos(self, msg):
        with self._data_lock:
            self.global_pos = msg
            self.last_update_time = time.time()
    
    def cb_wp_reached(self, msg):
        with self._data_lock:
            self.current_wp_reached = msg.wp_seq

    def get_data(self):
        with self._data_lock:
            return {
                "state": self.state,
                "pose": self.pose,
                "velocity": self.vel,
                "ext_state": self.ext_state,
                "status_text": self.last_status_text,
                "severity": self.last_status_severity,
                "global_pos": self.global_pos,
                "timestamp": self.last_update_time
            }
    def clear_errors(self):
        """每次 Fuzz 迭代开始前清理旧错误"""
        with self._data_lock:
            self.last_status_text = ""
            self.last_status_severity = 255

    # --- 服务调用（已修复 Future.result 问题） ---
    def set_param(self, name, value, timeout=2.0):
        req = ParamSetV2.Request()
        req.param_id = name
        
        if isinstance(value, bool):
            req.value.bool_value = value
            req.value.type = ParameterType.PARAMETER_BOOL
        elif isinstance(value, int):
            req.value.integer_value = int(value)
            req.value.type = ParameterType.PARAMETER_INTEGER
        else:
            req.value.double_value = float(value)
            req.value.type = ParameterType.PARAMETER_DOUBLE

        future = self.s_set_param.call_async(req)
        resp = self._wait_for_future(future, timeout)
        return resp is not None and resp.success

    def get_param(self, name, timeout=2.0):
        req = GetParameters.Request(names=[name])
        future = self.s_get_param.call_async(req)
        res = self._wait_for_future(future, timeout)
        
        if res and res.values:
            v = res.values[0]
            if v.type == ParameterType.PARAMETER_DOUBLE: return v.double_value
            if v.type == ParameterType.PARAMETER_INTEGER: return v.integer_value
            if v.type == ParameterType.PARAMETER_BOOL: return v.bool_value
        return None

    def snapshot_params(self, names):
        results = {}
        for n in names:
            val = self.get_param(n)
            if val is not None:
                results[n] = val
        return results

    def wait_services(self, timeout=10.0):
        self.node.get_logger().info("Waiting for MAVROS services...")
        services = [self.s_get_param, self.s_set_param, self.s_arm, self.s_mode]
        for s in services:
            if not s.wait_for_service(timeout_sec=timeout):
                raise RuntimeError(f"Service {s.srv_name} not available!")

    def set_mode(self, mode: str, timeout=5.0):
        req = SetMode.Request(custom_mode=mode)
        future = self.s_mode.call_async(req)
        resp = self._wait_for_future(future, timeout)
        return resp is not None and resp.mode_sent

    def arm(self, state: bool, timeout=5.0):
        req = CommandBool.Request(value=state)
        future = self.s_arm.call_async(req)
        resp = self._wait_for_future(future, timeout)
        return resp is not None and resp.success

    def auto_takeoff(self, target_alt, timeout=30.0):
        self.set_param("MIS_TAKEOFF_ALT", float(target_alt))
        if not self.set_mode("AUTO.TAKEOFF"):
            return False
        if not self.arm(True):
            return False

        start_t = time.time()
        while time.time() - start_t < timeout:
            with self._data_lock:
                if self.pose and self.pose.pose.position.z >= (target_alt - 0.3):
                    self.node.get_logger().info("Takeoff target reached.")
                    return True
            time.sleep(0.5)
        return False

    def land(self, timeout=40.0):
        if not self.set_mode("AUTO.LAND"):
            return False
        
        # 软件在环不是物理检查，着陆不准确，给一点时间着陆
        #time.sleep(4.0)

        start_t = time.time()
        while time.time() - start_t < timeout:
            with self._data_lock:
                if not self.state.armed:
                    self.node.get_logger().info("Success: Disarmed by PX4 landing logic.")
                    return True
                # if self.ext_state.landed_state == 2:
                #     self.node.get_logger().info("Ground contact detected. Waiting for 2s stabilization...")
                #     if self.state.armed:
                #         self.node.get_logger().info("Still armed, sending manual disarm...")
                #         self.arm(False)
            time.sleep(1.0)
        self.node.get_logger().warn("⏰ Landing timeout! Force disarming now...")
        self.arm(False) # 超时强制断桨
        return False