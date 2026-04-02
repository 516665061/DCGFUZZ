#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from rcl_interfaces.srv import ListParameters, GetParameters


class PX4ParamDumper(Node):
    def __init__(self, filename="px4_param_values.txt"):
        super().__init__('px4_param_dumper')
        self.filename = filename

        # 服务接口
        self.cli_list = self.create_client(ListParameters, '/mavros/param/list_parameters')
        self.cli_get = self.create_client(GetParameters, '/mavros/param/get_parameters')

        self.get_logger().info("等待 MAVROS 参数服务可用...")
        self.cli_list.wait_for_service()
        self.cli_get.wait_for_service()
        self.get_logger().info("服务已连接！")

        self.dump()

    def dump(self):
        # 先获取参数名
        req = ListParameters.Request()
        future = self.cli_list.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        result = future.result()

        if result is None:
            self.get_logger().error("❌ 无法获取参数名称")
            return

        names = sorted(result.result.names)

        self.get_logger().info(f"🔍 共找到 {len(names)} 个参数，开始读取数值...")

        param_values = {}

        # 再逐个读取参数值
        for name in names:
            req_val = GetParameters.Request()
            req_val.names.append(name)

            future_val = self.cli_get.call_async(req_val)
            rclpy.spin_until_future_complete(self, future_val)
            result_val = future_val.result()

            if result_val and result_val.values:
                value = result_val.values[0]
                # 根据类型提取值
                if value.type == 1:
                    param_values[name] = value.bool_value
                elif value.type == 2:
                    param_values[name] = value.integer_value
                elif value.type == 3:
                    param_values[name] = value.double_value
                elif value.type == 4:
                    param_values[name] = value.string_value
                else:
                    param_values[name] = "<unknown type>"

        # 保存到文件
        with open(self.filename, "w") as f:
            for k, v in param_values.items():
                f.write(f"{k}:{v}\n")

        # with open(self.filename, "w") as f:
        #     for k in names:
        #         f.write(f"{k}\n")

        self.get_logger().info(f"📄 参数名称 + 数值已保存到 {self.filename}")


def main():
    rclpy.init()
    node = PX4ParamDumper()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
