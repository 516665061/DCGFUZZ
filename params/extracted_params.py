import json
import os

# === 文件配置 ===
WHITELIST_FILE = "px4_param_values.txt"  # 你从 PX4 导出的实际参数名列表
DATABASE_FILE = "parameters.json"         # 原始大数据库文件
OUTPUT_FILE = "full_extracted_parameters.json"  # 输出文件

def run_full_extraction():
    # 1. 加载白名单 (去重、过滤空行、统一大写防止匹配失败)
    if not os.path.exists(WHITELIST_FILE):
        print(f"❌ 找不到白名单文件: {WHITELIST_FILE}")
        return
    
    param_value_map = {}
    with open(WHITELIST_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            
            # 按照冒号分割，例如 ASPD_SCALE_1:1.0
            parts = line.split(":")
            name = parts[0].strip().upper()
            raw_value = parts[1].strip()
            
            # 尝试转换数值类型，方便后续分析
            try:
                if "." in raw_value:
                    value = float(raw_value)
                else:
                    value = int(raw_value)
            except ValueError:
                value = raw_value  # 如果不是数字则保留字符串
                
            param_value_map[name] = value
    
    whitelist_names = set(param_value_map.keys())
    print(f"📖 已加载 {len(whitelist_names)} 个来自固件的参数名及当前值。")

    # 2. 加载原始参数数据库
    if not os.path.exists(DATABASE_FILE):
        print(f"❌ 找不到数据库文件: {DATABASE_FILE}")
        return

    with open(DATABASE_FILE, 'r') as f:
        try:
            db_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 格式错误: {e}")
            return

    # 获取参数列表
    all_params = db_data.get("parameters", [])
    print(f"🔍 数据库总记录数: {len(all_params)}")

    # 3. 执行全量提取
    extracted_results = []
    found_names = set()

    for p in all_params:
        # 统一转大写匹配
        p_name = p.get("name", "").upper()
        
        if p_name in whitelist_names:
            # 拷贝一份数据防止修改原始对象（如果需要多次处理）
            param_entry = p.copy()
            # 注入从 txt 中提取的当前值
            decimal = p.get("decimal")
            p_type = p.get("type")
            if p_type == "Int32":
                param_entry["cur"] = int(param_value_map[p_name])
            elif decimal is not None:
                param_entry["cur"] = round(float(param_value_map[p_name]), decimal)
            else:
                param_entry["cur"] = round(float(param_value_map[p_name]), 4)  # 默认兜底 4 位
            # 保持原始数据的完整性，不做任何删减
            extracted_results.append(param_entry)
            found_names.add(p_name)

    # 4. 统计未找到的参数
    missing = whitelist_names - found_names

    # 5. 保存结果
    # 保持与原始 parameters.json 一致的结构，方便后续脚本直接调用
    output_data = {
        "parameters": extracted_results,
        "version": db_data.get("version", 1),
        "count": len(extracted_results)
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output_data, f, indent=4)

    print("\n" + "="*40)
    print(f"✅ 全量提取完成！")
    print(f"📦 匹配成功的参数数量: {len(extracted_results)}")
    if missing:
        print(f"⚠️ 固件中有但数据库里没有的参数: {len(missing)} 个")
        # 如果需要查看缺失列表，可以取消下面这一行的注释
        # print(f"缺失列表示例: {list(missing)[:5]}") 
    print(f"💾 完整定义已保存至: {OUTPUT_FILE}")
    print("="*40)

if __name__ == "__main__":
    run_full_extraction()