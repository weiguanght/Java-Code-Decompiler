#!/usr/bin/env python3
"""
Smali/Class 信息提取工具
使用 javap 从 JAR 中提取原始类信息和方法签名
"""

import subprocess
import re
from typing import Optional, List, Dict
from dataclasses import dataclass


@dataclass
class MethodSignature:
    name: str
    descriptor: str  # e.g. ()I, (Ljava/lang/String;)V
    return_type: str
    param_types: List[str]


@dataclass
class ClassInfo:
    class_name: str
    is_interface: bool
    super_class: str
    interfaces: List[str]
    methods: List[MethodSignature]
    fields: List[str]


JAR_PATH = '/Users/hoto/PC_Java/Original_Source/game-lib.jar'


def parse_descriptor(desc: str) -> tuple:
    """解析 JVM 方法描述符"""
    # e.g. (Lcom/example/Foo;I)V -> (['com.example.Foo', 'int'], 'void')
    type_map = {
        'V': 'void', 'Z': 'boolean', 'B': 'byte', 'C': 'char',
        'S': 'short', 'I': 'int', 'J': 'long', 'F': 'float', 'D': 'double'
    }
    
    params = []
    returns = 'void'
    
    if desc.startswith('('):
        inner = desc[1:desc.index(')')]
        ret_part = desc[desc.index(')') + 1:]
        
        # 解析参数
        i = 0
        while i < len(inner):
            if inner[i] == 'L':
                end = inner.index(';', i)
                params.append(inner[i+1:end].replace('/', '.'))
                i = end + 1
            elif inner[i] == '[':
                # 数组类型
                i += 1
                continue
            else:
                params.append(type_map.get(inner[i], inner[i]))
                i += 1
        
        # 解析返回类型
        if ret_part.startswith('L'):
            returns = ret_part[1:-1].replace('/', '.')
        elif ret_part.startswith('['):
            returns = ret_part + '[]'
        else:
            returns = type_map.get(ret_part, ret_part)
    
    return params, returns


def get_class_info(obf_class_name: str, jar_path: str = JAR_PATH) -> Optional[ClassInfo]:
    """
    获取类的详细信息
    
    Args:
        obf_class_name: 混淆类名，如 'com.corrodinggames.rts.game.units.ak'
        jar_path: JAR 文件路径
    
    Returns:
        ClassInfo 对象或 None
    """
    try:
        result = subprocess.run(
            ['javap', '-v', '-classpath', jar_path, obf_class_name],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode != 0:
            return None
        
        output = result.stdout
        
        # 解析类信息
        is_interface = 'ACC_INTERFACE' in output
        
        # 提取父类
        super_match = re.search(r'super_class: #\d+\s+// (.+)', output)
        super_class = super_match.group(1).replace('/', '.') if super_match else 'java.lang.Object'
        
        # 提取方法
        methods = []
        method_pattern = re.compile(r'public abstract (\w+) (\w+)\((.*?)\);')
        descriptor_pattern = re.compile(r'descriptor: (.+)')
        
        lines = output.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if 'public abstract' in line or 'public' in line:
                # 查找方法名
                match = re.search(r'(\w+)\s+(\w+)\(', line)
                if match:
                    method_name = match.group(2)
                    # 查找描述符
                    for j in range(i, min(i + 5, len(lines))):
                        desc_match = descriptor_pattern.search(lines[j])
                        if desc_match:
                            descriptor = desc_match.group(1)
                            params, ret = parse_descriptor(descriptor)
                            methods.append(MethodSignature(
                                name=method_name,
                                descriptor=descriptor,
                                return_type=ret,
                                param_types=params
                            ))
                            break
            i += 1
        
        return ClassInfo(
            class_name=obf_class_name,
            is_interface=is_interface,
            super_class=super_class,
            interfaces=[],
            methods=methods,
            fields=[]
        )
    
    except Exception as e:
        print(f"Error: {e}")
        return None


def get_smali_of_class(obf_class_name: str) -> Optional[str]:
    """
    获取类的 Smali 风格伪代码表示（使用 javap 输出）
    
    Args:
        obf_class_name: 混淆类名
    
    Returns:
        Smali 风格的伪代码字符串
    """
    info = get_class_info(obf_class_name)
    if not info:
        return None
    
    lines = []
    lines.append(f".class public {'interface abstract ' if info.is_interface else ''}{obf_class_name.replace('.', '/')}")
    lines.append(f".super {info.super_class.replace('.', '/')}")
    lines.append("")
    
    for m in info.methods:
        lines.append(f".method public {'abstract ' if info.is_interface else ''}{m.name}{m.descriptor}")
        lines.append("    # " + f"Returns: {m.return_type}, Params: {m.param_types}")
        lines.append(".end method")
        lines.append("")
    
    return '\n'.join(lines)


def list_classes_in_jar(jar_path: str) -> List[str]:
    """列出 JAR 中的所有类"""
    import zipfile
    classes = []
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.class') and not '$' in name:  # 排除内部类
                    # 转换路径为类名
                    class_name = name[:-6].replace('/', '.')
                    classes.append(class_name)
    except Exception as e:
        print(f"Error listing JAR: {e}")
    return classes


def batch_extract(jar_path: str, output_dir: str):
    """批量提取所有类的 Smali 风格信息"""
    import os
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 获取所有类
    classes = list_classes_in_jar(jar_path)
    print(f"找到 {len(classes)} 个类")
    
    success = 0
    failed = 0
    
    for class_name in classes:
        smali = get_smali_of_class(class_name)
        if smali:
            # 创建输出文件路径
            relative_path = class_name.replace('.', '/') + '.smali'
            full_path = os.path.join(output_dir, relative_path)
            
            # 确保父目录存在
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            # 写入文件
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(smali)
            success += 1
        else:
            failed += 1
        
        if (success + failed) % 100 == 0:
            print(f"  进度: {success + failed}/{len(classes)}")
    
    print(f"完成! 成功: {success}, 失败: {failed}")


if __name__ == '__main__':
    import sys
    
    # 默认路径
    jar_path = '/Users/hoto/PC_Java/Original_Source/game-lib.jar'
    output_dir = '/Users/hoto/PC_Java/smali_output'
    
    # 支持命令行参数
    if len(sys.argv) > 1:
        jar_path = sys.argv[1]
    if len(sys.argv) > 2:
        output_dir = sys.argv[2]
    
    print(f"JAR 路径: {jar_path}")
    print(f"输出目录: {output_dir}")
    print()
    
    batch_extract(jar_path, output_dir)
