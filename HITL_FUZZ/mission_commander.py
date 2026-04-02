# mission_commander.py
import time
from mavros_msgs.msg import Waypoint, CommandCode
from mavros_msgs.srv import WaypointPush, WaypointClear

class MissionCommander:
    def __init__(self, px4_interface):
        self.px4 = px4_interface
        self.node = px4_interface.node
        self.srv_wp_push = self.node.create_client(WaypointPush, "/mavros/mission/push")
        self.srv_wp_clear = self.node.create_client(WaypointClear, "/mavros/mission/clear")

    def _wait_for_service(self, client, timeout=5.0):
        if not client.wait_for_service(timeout_sec=timeout):
            self.node.get_logger().error(f"Service {client.srv_name} not available")
            return False
        return True

    def upload_mission(self, alt=10.0):
        # 1. 检查 GPS
        if self.px4.global_pos is None:
            self.node.get_logger().error("MissionCMD: No GPS fix (global_pos is None).")
            return False
            
        home_lat = self.px4.global_pos.latitude
        home_lon = self.px4.global_pos.longitude
        print(f"home_lat: {home_lat}")
        print(f"home_lon: {home_lon}")

        # 2. 准备服务
        if not self._wait_for_service(self.srv_wp_clear) or not self._wait_for_service(self.srv_wp_push):
            return False

        # 3. 清除旧任务
        req_clr = WaypointClear.Request()
        clr_future = self.srv_wp_clear.call_async(req_clr)
        clr_resp = self.px4._wait_for_future(clr_future, timeout_sec=15.0)

        if clr_resp and clr_resp.success:
            self.node.get_logger().info("MissionCMD: Clear success.")
            # 增加关键延迟，给 PX4 状态机复位的时间
            time.sleep(4.0) 
        else:
            self.node.get_logger().error("MissionCMD: Clear failed.")
            return False

        wps = []

        # --- WP 0: 必须是 TAKEOFF (根据 PX4 feasibility checker 要求) ---
        # 修复说明：去掉之前的 Dummy Point，直接从起飞开始
        wp0 = Waypoint()
        wp0.frame = Waypoint.FRAME_GLOBAL_REL_ALT
        wp0.command = CommandCode.NAV_TAKEOFF
        wp0.is_current = True   # 第一个执行的点设置为 True
        wp0.autocontinue = True
        wp0.param1 = 15.0       # 最小俯仰角
        wp0.param4 = float('nan')
        wp0.x_lat = home_lat
        wp0.y_long = home_lon
        wp0.z_alt = alt
        wps.append(wp0)

        # --- WP 1-4: 正方形航点 ---
        offsets = [(0.0002, 0.0002), (-0.0002, 0.0002), (-0.0002, -0.0002), (0.0002, -0.0002)]
        for lat_off, lon_off in offsets:
            wp = Waypoint()
            wp.frame = Waypoint.FRAME_GLOBAL_REL_ALT
            wp.command = CommandCode.NAV_WAYPOINT
            wp.is_current = False
            wp.autocontinue = True
            wp.param1 = 0.0       # Hold time
            wp.param2 = 2.0       # Acceptance radius
            wp.param4 = float('nan')
            wp.x_lat = home_lat + lat_off
            wp.y_long = home_lon + lon_off
            wp.z_alt = alt
            wps.append(wp)

        # --- WP 5: LAND (降落) ---
        # land = Waypoint()
        # land.frame = Waypoint.FRAME_GLOBAL_REL_ALT
        # land.command = CommandCode.NAV_LAND
        # land.is_current = False
        # land.autocontinue = True
        # land.x_lat = home_lat
        # land.y_long = home_lon
        # land.z_alt = 0.0
        # wps.append(land)

        # --- WP 5: JUMP ---
        jump_wp = Waypoint()
        jump_wp.frame = Waypoint.FRAME_MISSION # MISSION 
        jump_wp.command = CommandCode.DO_JUMP # MAV_CMD_DO_JUMP
        jump_wp.is_current = False
        jump_wp.autocontinue = True
        jump_wp.param1 = 1.0
        jump_wp.param2 = 9999.0
        jump_wp.param3 = 0.0
        
        wps.append(jump_wp)

        # 4. 推送任务
        req_push = WaypointPush.Request()
        req_push.start_index = 0
        req_push.waypoints = wps
        
        self.node.get_logger().info(f"MissionCMD: Pushing {len(wps)} waypoints (Takeoff @ Index 0)...")

        future = self.srv_wp_push.call_async(req_push)
        resp = self.px4._wait_for_future(future, timeout_sec=25.0)
        
        if resp and resp.success:
            self.node.get_logger().info(f"MissionCMD: Upload SUCCESS.")
            return True
        else:
            self.node.get_logger().error("MissionCMD: Upload FAILED.")
            return False