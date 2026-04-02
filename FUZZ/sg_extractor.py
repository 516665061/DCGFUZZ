import json
import re
import os

# === 配置区 ===
INPUT_FILE = "full_extracted_parameters.json"
OUTPUT_FILE = "semantic_space.json"

# 1. 基础黑名单（保持不变，剔除掉完全无关的硬件/系统参数）
PREFIX_BLACKLIST = r"^(BAT[0-9]*_|CBRK_|FD_|GF_|RC[0-9]*_|TC_|CAL_|SYS_|UAVCAN_|PWM_|PCA9685_|SDLOG_|MAV_|TRIG_|SIM_|SIH_|HIL_)"
STRICT_BLACKLIST = r".*(_EN|_MODE|_TYPE|_ID|_CUT|_SAVE|_BIT|_FUNC|_HASH|ID$|HASH$)$"

# 2. 定义相位及其关联的关键字映射
# 逻辑：每个相位 = 自己的特有参数关键字 + COMMON (基础控制)
# === 优化后的相位映射 (兼顾状态感知与参数覆盖率) ===
PHASE_MAPPING = {
    "Takeoff": {
        "keywords": ["MPC_TKO_", "MPC_Z_", "MPC_THR_", "MPC_MAN_", "COM_TKO_", "MPC_TKO_RAMP"],
        "desc": "起飞相位：爬升速度、推力曲线及起飞逻辑。"
    },
    "Mission": {
        "keywords": ["NAV_", "MIS_", "MPC_XY_", "MPC_WAYPOINT", "MPC_ACC_", "MPC_JERK_", "MPC_YAW_MODE", "LNAV_"],
        "desc": "任务相位：航迹跟随、水平速度及转弯决策。"
    },
    "Hold": {
        "keywords": ["MPC_HOLD_", "MPC_VEL_P", "MPC_XY_P", "MPC_Z_P", "MPC_VEL_I"],
        "desc": "悬停相位：位置保持及静止抗扰动。"
    },
    "Landing": {
        "keywords": ["MPC_LAND_", "MPC_Z_VEL_MAX_DN", "COM_LND_", "LNDMC_"],
        "desc": "降落相位：下降速率及触地判定。"
    },
    "Common_Base": {
        "keywords": [
            "MC_ROLL", "MC_PITCH", "MC_YAW", "RATE_P", "RATE_I", "RATE_D", # PID核心
            "ATT_BW", "ATT_MAG", "IMU_GYRO_",                             # 姿态与传感器
            "EKF2_MAG_", "EKF2_AID_", "EKF2_HGT_", "EKF2_GPS_",           # EKF2核心权重
            "CA_MC_R", "CA_ACT_METHOD",                                   # 控制分配核心
            "MPC_XY_VEL_MAX", "MPC_Z_VEL_MAX"                             # 全局速度限制
        ],
        "desc": "底层控制与估算：影响全相位稳定性的核心参数。"
    }
}

def get_phases_for_param(param_name):
    """
    判断一个参数属于哪些相位
    """
    matched_phases = []
    for phase, info in PHASE_MAPPING.items():
        for kw in info["keywords"]:
            if kw in param_name:
                matched_phases.append(phase)
                break
    return matched_phases

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 错误：找不到输入文件 {INPUT_FILE}")
        return

    with open(INPUT_FILE, 'r') as f:
        raw_data = json.load(f)

    # 结果容器：按相位索引
    phase_buckets = {phase: {} for phase in PHASE_MAPPING.keys()}
    total_count = 0

    print(f"🔍 正在按飞行相位重组参数空间...")

    for p in raw_data.get("parameters", []):
        name = p.get("name", "")
        
        # 过滤黑名单
        if re.match(STRICT_BLACKLIST, name) or re.match(PREFIX_BLACKLIST, name):
            continue

        # 匹配相位
        matched_phases = get_phases_for_param(name)
        if not matched_phases:
            continue

        p_info = {
            "def": p.get("default"),
            "cur": p.get("cur"),
            "min": p.get("min"),
            "max": p.get("max"),
            "type": p.get("type", "Float"),
            "decimal": p.get("decimalPlaces"),
            "values": p.get("values"),
            "is_enum": (p.get("values") is not None and len(p.get("values")) > 0)
        }

        # 校验数据完整性
        if p_info["min"] is None or p_info["max"] is None:
            continue

        for ph in matched_phases:
            phase_buckets[ph][name] = p_info
        
        total_count += 1

    # --- 关键重组逻辑 ---
    # 将 Common_Base 合并到各个业务相位中，形成最终的 Fuzz 池
    final_fuzz_space = {}
    common_params = phase_buckets.pop("Common_Base") # 提取公共参数

    for phase, params in phase_buckets.items():
        # 每个阶段 = 阶段特有 + 公共底层
        combined_params = {**params, **common_params}
        final_fuzz_space[phase] = {
            "info": PHASE_MAPPING[phase]["desc"],
            "count": len(combined_params),
            "parameters": combined_params
        }

    # 保存
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(final_fuzz_space, f, indent=4)

    print("\n✅ 相位空间提取完成！")
    print("-" * 50)
    for phase, data in final_fuzz_space.items():
        print(f" 相位: {phase:10} | 综合参数量 (特有+基础): {data['count']:3}")
    print("-" * 50)
    print(f"总计去重后的核心参数: {total_count} (已保存至 {OUTPUT_FILE})")

if __name__ == "__main__":
    main()