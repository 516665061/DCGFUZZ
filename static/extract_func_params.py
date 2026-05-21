import os
import re
import json

# 配置你的 PX4 源码路径和输出路径
PX4_SRC_DIR = "/home/a1047/Project/PX4-Autopilot/src"
OUTPUT_DIR = "./px4_func_params"

# 新增：只扫描这两个核心目录
TARGET_SUBDIRS = ["modules", "lib"]

def build_parameter_mapping(src_dir):
    """
    第一步：扫描指定目录，建立 C++ 变量名到 PX4 参数名的字典映射
    """
    print(f"[*] 正在 {TARGET_SUBDIRS} 目录下构建参数映射字典...")
    mapping_table = {}
    
    # 匹配现代宏定义: (ParamFloat<px4::params::MC_ROLL_P>) _param_mc_roll_p
    modern_pattern = re.compile(r'\(Param[A-Za-z]+<px4::params::([A-Z0-9_]+)>\)\s*([a-zA-Z0-9_]+)')
    # 匹配传统调用: param_find("MPC_Z_P") -> 获取参数名本身
    legacy_pattern = re.compile(r'param_find\s*\(\s*"([A-Z0-9_]+)"\s*\)')

    # 修改点：只遍历指定的子目录
    for subdir in TARGET_SUBDIRS:
        target_path = os.path.join(src_dir, subdir)
        if not os.path.exists(target_path):
            print(f"⚠️ 找不到目录: {target_path}")
            continue

        for root, _, files in os.walk(target_path):
            for file in files:
                if file.endswith(('.cpp', '.hpp', '.h')):
                    filepath = os.path.join(root, file)
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                    except:
                        continue

                    for match in modern_pattern.finditer(content):
                        px4_param = match.group(1)
                        cpp_var = match.group(2)
                        mapping_table[cpp_var] = px4_param

    print(f"✅ 映射字典构建完毕！共提取了 {len(mapping_table)} 个映射对。")
    return mapping_table

def find_matching_brace(code, start_index):
    """括号匹配，安全提取函数体"""
    stack = []
    for i in range(start_index, len(code)):
        if code[i] == '{': 
            stack.append('{')
        elif code[i] == '}':
            stack.pop()
            if not stack: 
                return i
    return -1

def extract_params_from_cpp(src_dir, output_dir, mapping_table):
    """
    第二步：逐个解析指定目录下的 cpp 文件，提取函数及其使用的参数
    """
    print(f"\n[*] 正在 {TARGET_SUBDIRS} 目录下扫描函数并提取使用参数...")
    
    # 匹配 C++ 函数定义的正则
    function_pattern = re.compile(
        r"""(?<!\w)(void|bool|int|float|double|char|short|long|unsigned|signed)[\*\&\s]+(?:[a-zA-Z0-9_]+::)?([a-zA-Z0-9_]+)\s*\([^)]*\)\s*(const)?\s*\{""", 
        re.VERBOSE | re.DOTALL
    )

    files_processed = 0

    # 修改点：只遍历指定的子目录
    for subdir in TARGET_SUBDIRS:
        target_path = os.path.join(src_dir, subdir)
        if not os.path.exists(target_path):
            continue

        for root, _, files in os.walk(target_path):
            for file in files:
                if file.endswith('.cpp'):
                    filepath = os.path.join(root, file)
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            code = f.read()
                    except:
                        continue

                    # 移除注释，防止注释里的废弃代码干扰
                    code = re.sub(r'^\s*//.*$', '', code, flags=re.MULTILINE)
                    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)

                    file_func_map = {}

                    for match in function_pattern.finditer(code):
                        start_index = match.start()
                        brace_index = code.find('{', start_index)
                        end_index = find_matching_brace(code, brace_index)
                        
                        if end_index == -1: 
                            continue

                        function_name = match.group(2)
                        func_body = code[brace_index + 1 : end_index]

                        # 1. 提取函数体内的所有独立单词(标识符)
                        tokens = re.findall(r'\b[a-zA-Z_]\w*\b', func_body)
                        
                        # 2. 提取字面量中的大写参数名 (例如 param_find("MPC_Z_P"))
                        literal_params = re.findall(r'"([A-Z0-9_]{3,})"', func_body)

                        used_params = set()
                        
                        # 检查 Token 是否在映射字典中
                        for token in tokens:
                            if token in mapping_table:
                                used_params.add(mapping_table[token])
                                
                        # 检查字面量是否是大写的参数名
                        for p in literal_params:
                            # 简单的启发式：通常 PX4 参数名包含下划线且纯大写
                            if '_' in p and p.isupper():
                                used_params.add(p)

                        if used_params:
                            file_func_map[function_name] = list(used_params)

                    # 如果这个文件里解析到了参数，就保存为 JSON
                    if file_func_map:
                        # 保持相对路径结构 (例如 modules/mc_att_control/...)
                        relative_path = os.path.relpath(root, src_dir)
                        save_dir = os.path.join(output_dir, relative_path)
                        os.makedirs(save_dir, exist_ok=True)
                        
                        json_name = file.replace('.cpp', '.json')
                        json_path = os.path.join(save_dir, json_name)
                        
                        with open(json_path, 'w', encoding='utf-8') as jf:
                            json.dump(file_func_map, jf, indent=4)
                        
                        files_processed += 1

    print(f"✅ 提取完成！共生成 {files_processed} 个 JSON 文件，保存在 {output_dir} 目录下。")

if __name__ == "__main__":
    # 1. 先建表
    param_map = build_parameter_mapping(PX4_SRC_DIR)
    
    # 2. 后扫描提取
    extract_params_from_cpp(PX4_SRC_DIR, OUTPUT_DIR, param_map)