import os
import json
import glob

def transform_param_info(p):
    """
    提取参数信息并执行 20% 极差扩张
    """
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

    if p_info["type"] == "Int32":
        if p_info["min"] is None: p_info["min"] = -2147483648
        if p_info["max"] is None: p_info["max"] = 2147483647
    elif p_info["type"] == "Float":
        if p_info["min"] is None: p_info["min"] = -1e6
        if p_info["max"] is None: p_info["max"] = 1e6
    
    try:
        range_span = float(p_info["max"]) - float(p_info["min"])
        if p_info["type"] == "Int32" and not p_info["is_enum"]:
            if p_info["min"] != -2147483648:
                p_info["min"] = int(p_info["min"] - range_span * 0.2)
            if p_info["max"] != 2147483647:
                p_info["max"] = int(p_info["max"] + range_span * 0.2)
                
        elif p_info["type"] == "Float" and not p_info["is_enum"]:
            if p_info["min"] != -1e6:
                p_info["min"] = round(float(p_info["min"] - range_span * 0.2), p_info.get("decimal", 4))
            if p_info["max"] != 1e6:
                p_info["max"] = round(float(p_info["max"] + range_span * 0.2), p_info.get("decimal", 4))
    except (TypeError, ValueError):
        pass

    return p_info

def merge_and_enrich_paths(input_jsons_dir, reference_json_file, output_json_file):
    print(f"📥 正在加载参考字典: {reference_json_file}...")
    if not os.path.exists(reference_json_file):
        print(f"❌ 错误：找不到参考文件 {reference_json_file}")
        return

    # ==========================================
    # 【终极修复：全自动参数挖掘机】
    # ==========================================
    valid_params_db = {}
    with open(reference_json_file, 'r', encoding='utf-8') as f:
        raw_db = json.load(f)
        
        # 1. 如果最外层是列表
        if isinstance(raw_db, list):
            for item in raw_db:
                if isinstance(item, dict) and "name" in item:
                    valid_params_db[item["name"]] = item
                    
        # 2. 如果最外层是字典 (像你这次一样，只有3个键)
        elif isinstance(raw_db, dict):
            # 遍历所有键，找找看哪个键对应的是列表，且里面有我们需要的字典
            for key, value in raw_db.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "name" in item:
                            valid_params_db[item["name"]] = item
            
            # 如果上面没找到，说明它可能本身就是一个大字典 {"MC_YAW_WEIGHT": {...}}
            if len(valid_params_db) == 0:
                valid_params_db = raw_db

    print(f"🔧 [成功] 真理库加载完毕，智能挖出 {len(valid_params_db)} 个有效参数！")

    if len(valid_params_db) < 10:
        print("⚠️ 警告：提取的参数太少了，请检查 full_extracted_parameters.json 里的数据是不是空的。")

    # ==========================================
    # 路径合并逻辑
    # ==========================================
    merged_paths = {}
    abs_input_dir = os.path.abspath(input_jsons_dir)
    search_pattern = os.path.join(abs_input_dir, "**", "*.json")
    json_files = glob.glob(search_pattern, recursive=True)
    
    if not json_files:
        print(f"⚠️ 在目录及子目录 {abs_input_dir} 中没有找到任何 JSON 文件。")
        return

    print(f"🔍 深度扫描找到 {len(json_files)} 个源文件，开始处理...")

    for file_path in json_files:
        if os.path.abspath(file_path) in [os.path.abspath(output_json_file), os.path.abspath(reference_json_file)]:
            continue
            
        rel_path = os.path.relpath(file_path, abs_input_dir)
        original_cpp_path = rel_path.replace('.json', '.cpp').replace('\\', '/')
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                file_data = json.load(f)
            
            if not isinstance(file_data, dict): continue

            for func_name, params_list in file_data.items():
                path_id = f"{original_cpp_path}::{func_name}"
                valid_path_params = {}
                
                if not isinstance(params_list, list): continue

                for param_name in params_list:
                    # 兼容不同格式的参数名提取
                    actual_name = param_name["name"] if isinstance(param_name, dict) and "name" in param_name else str(param_name)
                    
                    if actual_name in valid_params_db:
                        raw_p = valid_params_db[actual_name]
                        valid_path_params[actual_name] = transform_param_info(raw_p)
                
                if valid_path_params:
                    merged_paths[path_id] = {
                        "source_file": original_cpp_path,
                        "function": func_name,
                        "parameters": valid_path_params
                    }
        except Exception as e:
            print(f"⚠️ 处理文件 {os.path.basename(file_path)} 时出错: {e}")

    # ==========================================
    # 对合并后的路径按参数数量由高到低进行排序
    # ==========================================
    sorted_merged_paths = dict(sorted(
        merged_paths.items(), 
        key=lambda item: len(item[1]["parameters"]), 
        reverse=True
    ))

    with open(output_json_file, 'w', encoding='utf-8') as f:
        json.dump(sorted_merged_paths, f, indent=4)
    print(f"✅ 处理完成！共生成 {len(sorted_merged_paths)} 条有效路径。")
    print(f"💾 结果保存至: {output_json_file}")

if __name__ == "__main__":
    INPUT_DIR = "./px4_func_params"  
    REF_FILE = "./full_extracted_parameters.json" 
    OUT_FILE = "./merged_paths_master.json"

    merge_and_enrich_paths(INPUT_DIR, REF_FILE, OUT_FILE)