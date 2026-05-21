import os
import json

# 配置之前生成的 JSON 目录和最终要输出的总文件路径
INPUT_DIR = "./px4_func_params"
OUTPUT_FILE = "./px4_global_functions.json"

def merge_and_sort_json(input_dir, output_file):
    print(f"[*] 正在扫描目录 {input_dir} 下的所有 JSON 文件...")
    merged_data = {}
    files_scanned = 0

    # 1. 遍历并合并数据
    for root, _, files in os.walk(input_dir):
        for file in files:
            if file.endswith('.json'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        files_scanned += 1
                        
                        # 按函数名合并参数
                        for func_name, params in data.items():
                            if func_name not in merged_data:
                                # 使用 set 自动去重
                                merged_data[func_name] = set()
                            merged_data[func_name].update(params)
                except Exception as e:
                    print(f"⚠️ 读取文件失败: {filepath}, 错误: {e}")

    print(f"[*] 扫描完毕！共处理了 {files_scanned} 个 JSON 文件。")
    print(f"[*] 正在进行参数去重与降序排序...")

    # 2. 转换 set 为 list，并按参数数量降序排序 (参数最多的函数排在最上面)
    sorted_data = {
        func_name: list(params_set) 
        for func_name, params_set in sorted(
            merged_data.items(), 
            key=lambda item: len(item[1]), # 按 params_set 的长度排序
            reverse=True                   # 降序
        )
    }

    # 3. 写入最终的总 JSON 文件
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(sorted_data, f, indent=4)
        print(f"✅ 成功提取出 {len(sorted_data)} 个独立函数！")
        print(f"🚀 最终的总览表已保存至: {output_file}")
    except Exception as e:
        print(f"❌ 写入文件失败: {e}")

if __name__ == "__main__":
    if not os.path.exists(INPUT_DIR):
        print(f"❌ 错误: 找不到输入目录 {INPUT_DIR}，请先运行上一个提取脚本。")
    else:
        merge_and_sort_json(INPUT_DIR, OUTPUT_FILE)