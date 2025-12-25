"""
Native 方法映射收集器

功能:
- 收集所有 native 方法声明
- 生成 JNI 函数名映射表
- 辅助逆向分析
"""

import os
import re
from typing import Dict, List


def collect_native_methods(code: str, class_name: str) -> List[dict]:
    """
    收集代码中的 native 方法
    
    Args:
        code: Java 源代码
        class_name: 完整类名（点分隔）
        
    Returns:
        native 方法信息列表
    """
    pattern = re.compile(
        r'(?:public|private|protected)?\s*(?:static)?\s*native\s+'
        r'(\w+(?:\[\])?)\s+(\w+)\s*\(([^)]*)\)',
        re.MULTILINE
    )
    
    results = []
    for m in pattern.finditer(code):
        ret_type, method_name, params = m.groups()
        jni_name = generate_jni_name(class_name, method_name)
        results.append({
            'class': class_name,
            'method': method_name,
            'return_type': ret_type,
            'params': params.strip(),
            'jni_name': jni_name
        })
    return results


def generate_jni_name(class_name: str, method_name: str) -> str:
    """
    生成 JNI 函数名
    
    格式: Java_com_example_Class_methodName
    
    Args:
        class_name: 完整类名（点分隔）
        method_name: 方法名
        
    Returns:
        JNI 函数名
    """
    # 转义特殊字符
    escaped_class = class_name.replace('.', '_').replace('$', '_00024')
    escaped_method = method_name.replace('_', '_1')
    return f"Java_{escaped_class}_{escaped_method}"


def scan_directory_for_natives(source_dir: str, class_map: Dict[str, str] = None) -> List[dict]:
    """
    扫描目录收集所有 native 方法
    
    Args:
        source_dir: Java 源代码目录
        class_map: 可选的类映射表 {obf: orig}
        
    Returns:
        所有 native 方法信息列表
    """
    all_methods = []
    
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith('.java'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, source_dir)
                
                # 从路径推断类名
                class_name = rel_path.replace('/', '.').replace('.java', '')
                
                # 如果有映射表，使用原始类名
                if class_map and class_name in class_map:
                    orig_class_name = class_map[class_name]
                else:
                    orig_class_name = class_name
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    code = f.read()
                
                methods = collect_native_methods(code, orig_class_name)
                all_methods.extend(methods)
    
    return all_methods


def export_native_mapping(all_methods: List[dict], output_path: str, format_type: str = 'txt'):
    """
    导出 native 方法映射表
    
    Args:
        all_methods: native 方法列表
        output_path: 输出文件路径
        format_type: 输出格式 ('txt', 'json', 'csv')
    """
    if format_type == 'json':
        import json
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(all_methods, f, indent=2, ensure_ascii=False)
    
    elif format_type == 'csv':
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("jni_name,class,method,return_type,params\n")
            for m in all_methods:
                params = m['params'].replace(',', ';')  # 避免 CSV 分隔符冲突
                f.write(f"{m['jni_name']},{m['class']},{m['method']},{m['return_type']},{params}\n")
    
    else:  # txt
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("# Native Method Mapping\n")
            f.write("# Generated for JNI reverse engineering\n")
            f.write(f"# Total methods: {len(all_methods)}\n\n")
            
            # 按类分组
            by_class = {}
            for m in all_methods:
                cls = m['class']
                if cls not in by_class:
                    by_class[cls] = []
                by_class[cls].append(m)
            
            for cls in sorted(by_class.keys()):
                f.write(f"=== {cls} ===\n")
                for m in by_class[cls]:
                    sig = f"{m['return_type']} {m['method']}({m['params']})"
                    f.write(f"  {m['jni_name']}\n")
                    f.write(f"    -> {sig}\n")
                f.write("\n")


def main():
    """主函数 - 独立运行时使用"""
    import sys
    
    # 默认路径
    source_dir = '/Users/hoto/PC_Java/processed_output'
    output_path = '/Users/hoto/PC_Java/native_mapping.txt'
    mapping_file = '/Users/hoto/PC_Java/mappings.txt'
    
    print("=== Native 方法映射收集器 ===")
    print(f"源目录: {source_dir}")
    print(f"输出文件: {output_path}")
    
    # 加载类映射（可选）
    class_map = {}
    if os.path.exists(mapping_file):
        print(f"加载映射文件: {mapping_file}")
        with open(mapping_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if ' -> ' in line and not line.startswith(' '):
                    parts = line.rstrip(':').split(' -> ')
                    if len(parts) == 2:
                        class_map[parts[0]] = parts[1]
    
    print(f"类映射数: {len(class_map)}")
    print()
    
    print("扫描 native 方法...")
    methods = scan_directory_for_natives(source_dir, class_map)
    print(f"  找到 {len(methods)} 个 native 方法")
    
    print(f"导出映射表: {output_path}")
    export_native_mapping(methods, output_path, 'txt')
    
    # 同时导出 JSON 格式
    json_path = output_path.replace('.txt', '.json')
    export_native_mapping(methods, json_path, 'json')
    print(f"导出 JSON: {json_path}")
    
    print()
    print("完成!")


if __name__ == '__main__':
    main()
