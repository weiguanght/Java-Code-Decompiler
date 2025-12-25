#!/usr/bin/env python3
"""
JAR 字节码分析器

从 Java JAR 文件中提取完整的字节码信息，生成类似 Smali 的格式
包含方法体内的指令（调用、字段访问等）

使用 javap 工具反汇编 class 文件
"""

import os
import re
import subprocess
import zipfile
import tempfile
import shutil
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed


class JarBytecodeExtractor:
    """JAR 字节码提取器"""
    
    def __init__(self, jar_path: str, output_dir: str):
        self.jar_path = jar_path
        self.output_dir = output_dir
        
        self.stats = {
            'classes_extracted': 0,
            'methods_found': 0,
            'invoke_instructions': 0,
            'field_instructions': 0,
        }
    
    def extract_all(self, max_workers: int = 4) -> None:
        """提取 JAR 中所有类的字节码"""
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 创建临时目录解压 JAR
        temp_dir = tempfile.mkdtemp()
        
        try:
            # 解压 JAR
            print(f"解压 JAR 文件: {self.jar_path}")
            with zipfile.ZipFile(self.jar_path, 'r') as jar:
                jar.extractall(temp_dir)
            
            # 查找所有 class 文件
            class_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith('.class'):
                        class_path = os.path.join(root, file)
                        rel_path = os.path.relpath(class_path, temp_dir)
                        class_files.append((class_path, rel_path))
            
            print(f"发现 {len(class_files)} 个类文件")
            
            # 并行处理
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self._process_class, class_path, rel_path, temp_dir): rel_path
                    for class_path, rel_path in class_files
                }
                
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        print(f"处理失败: {futures[future]}: {e}")
            
            print(f"\n=== 提取完成 ===")
            print(f"  类数: {self.stats['classes_extracted']}")
            print(f"  方法数: {self.stats['methods_found']}")
            print(f"  调用指令数: {self.stats['invoke_instructions']}")
            print(f"  字段访问指令数: {self.stats['field_instructions']}")
            
        finally:
            # 清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    def _process_class(self, class_path: str, rel_path: str, temp_dir: str) -> None:
        """处理单个类文件"""
        # 使用 javap 反汇编
        try:
            result = subprocess.run(
                ['javap', '-c', '-p', '-s', class_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                return
            
            bytecode = result.stdout
            
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return
        
        # 解析并转换为 Smali 格式
        smali_content = self._convert_to_smali(bytecode, rel_path)
        
        if smali_content:
            # 保存到输出目录
            smali_path = os.path.join(
                self.output_dir,
                rel_path.replace('.class', '.smali')
            )
            os.makedirs(os.path.dirname(smali_path), exist_ok=True)
            
            with open(smali_path, 'w', encoding='utf-8') as f:
                f.write(smali_content)
            
            self.stats['classes_extracted'] += 1
    
    def _convert_to_smali(self, javap_output: str, rel_path: str) -> str:
        """将 javap 输出转换为 Smali 格式"""
        lines = javap_output.split('\n')
        smali_lines = []
        
        # 提取类名
        class_name = rel_path.replace('.class', '').replace('/', '.')
        smali_class = 'L' + class_name.replace('.', '/') + ';'
        
        smali_lines.append(f".class public {smali_class}")
        smali_lines.append(".super Ljava/lang/Object;")
        smali_lines.append("")
        
        current_method = None
        in_code = False
        
        for line in lines:
            line_stripped = line.strip()
            
            # 检测方法定义
            method_match = re.match(
                r'(?:public|private|protected|static|final|synchronized|native|abstract|\s)*'
                r'(\S+)\s+(\w+)\((.*?)\);',
                line_stripped
            )
            
            if method_match:
                ret_type = method_match.group(1)
                method_name = method_match.group(2)
                params = method_match.group(3)
                
                # 转换为 Smali 格式
                smali_ret = self._java_type_to_smali(ret_type)
                smali_params = self._params_to_smali(params)
                
                if current_method:
                    smali_lines.append(".end method")
                    smali_lines.append("")
                
                smali_lines.append(f".method public {method_name}({smali_params}){smali_ret}")
                current_method = method_name
                self.stats['methods_found'] += 1
                continue
            
            # 检测 Signature（JVM 签名）
            sig_match = re.match(r'descriptor:\s*(.+)', line_stripped)
            if sig_match:
                smali_lines.append(f"    # Descriptor: {sig_match.group(1)}")
                continue
            
            # 检测 Code 块
            if line_stripped == 'Code:':
                in_code = True
                continue
            
            if in_code:
                # 检测 invoke 指令
                invoke_match = re.search(
                    r'invoke(\w+)\s+#\d+\s+//\s+Method\s+(.+)',
                    line_stripped
                )
                if invoke_match:
                    invoke_type = invoke_match.group(1)
                    method_ref = invoke_match.group(2)
                    smali_lines.append(f"    invoke-{invoke_type.lower()} {method_ref}")
                    self.stats['invoke_instructions'] += 1
                    continue
                
                # 检测字段访问
                field_match = re.search(
                    r'(get|put)(field|static)\s+#\d+\s+//\s+Field\s+(.+)',
                    line_stripped
                )
                if field_match:
                    access = field_match.group(1)
                    static = field_match.group(2)
                    field_ref = field_match.group(3)
                    prefix = 's' if static == 'static' else 'i'
                    smali_lines.append(f"    {prefix}{access} {field_ref}")
                    self.stats['field_instructions'] += 1
                    continue
                
                # 检测代码块结束
                if re.match(r'^\d+:', line_stripped) is None and line_stripped and not line_stripped.startswith('#'):
                    if not any(kw in line_stripped.lower() for kw in ['stack', 'locals', 'args', 'linenum', 'exception']):
                        in_code = False
        
        if current_method:
            smali_lines.append(".end method")
        
        return '\n'.join(smali_lines)
    
    def _java_type_to_smali(self, java_type: str) -> str:
        """将 Java 类型转换为 Smali 类型"""
        type_map = {
            'void': 'V', 'boolean': 'Z', 'byte': 'B', 'char': 'C',
            'short': 'S', 'int': 'I', 'long': 'J', 'float': 'F', 'double': 'D'
        }
        
        if java_type in type_map:
            return type_map[java_type]
        
        # 数组类型
        if java_type.endswith('[]'):
            return '[' + self._java_type_to_smali(java_type[:-2])
        
        # 对象类型
        return 'L' + java_type.replace('.', '/') + ';'
    
    def _params_to_smali(self, params: str) -> str:
        """将参数列表转换为 Smali 格式"""
        if not params.strip():
            return ''
        
        result = ''
        for param in params.split(','):
            param = param.strip().split()[-1] if param.strip() else ''
            if param:
                # 移除参数名，只保留类型
                type_only = param.split()[0] if ' ' in param else param
                result += self._java_type_to_smali(type_only)
        
        return result


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='从 JAR 提取字节码')
    parser.add_argument('--jar', default='/Users/hoto/PC_Java/Original_Source/game-lib.jar', help='JAR 文件路径')
    parser.add_argument('--output', default='/Users/hoto/PC_Java/smali_full_output', help='输出目录')
    parser.add_argument('--workers', type=int, default=4, help='并行工作线程数')
    
    args = parser.parse_args()
    
    print("=== JAR 字节码提取器 ===\n")
    
    extractor = JarBytecodeExtractor(args.jar, args.output)
    extractor.extract_all(max_workers=args.workers)


if __name__ == '__main__':
    main()
