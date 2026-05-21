import os
import json

# 配置之前生成的 JSON 目录
INPUT_DIR = "./px4_func_params"

def analyze_json_stats(input_dir):
    print(f"[*] 正在统计目录 {input_dir} 下的数据...")
    
    total_function_occurrences = 0  # 合并前的函数总数
    merged_data = {}                # 用于存放合并后的函数
    all_unique_params = set()       # 用于存放所有的独立参数种类

    files_scanned = 0

    for root, _, files in os.walk(input_dir):
        for file in files:
            if file.endswith('.json'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        files_scanned += 1
                        
                        for func_name, params in data.items():
                            total_function_occurrences += 1
                            
                            # 统计合并后的函数
                            if func_name not in merged_data:
                                merged_data[func_name] = set()
                            merged_data[func_name].update(params)
                            
                            # 统计独立的参数种类
                            all_unique_params.update(params)
                except Exception as e:
                    print(f"⚠️ 读取文件失败: {filepath}, 错误: {e}")

    print("\n" + "="*40)
    print("📊 [ 全局扫描统计结果 ]")
    print("="*40)
    print(f"📁 共扫描 JSON 文件数 : {files_scanned} 个")
    print(f"1️⃣ 合并前函数总出现次数 : {total_function_occurrences} 个")
    print(f"2️⃣ 去重合并后独立函数数 : {len(merged_data)} 个")
    print(f"3️⃣ 全局涉及独立参数种类 : {len(all_unique_params)} 种")
    print("="*40 + "\n")

if __name__ == "__main__":
    if not os.path.exists(INPUT_DIR):
        print(f"❌ 错误: 找不到输入目录 {INPUT_DIR}，请确保路径正确。")
    else:
        analyze_json_stats(INPUT_DIR)