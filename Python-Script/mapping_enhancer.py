#!/usr/bin/env python3
"""
增强映射生成器

基于 Smali 分析和 DeepEnhancer 推断结果，生成增强的 mappings.txt

功能:
1. 读取现有映射
2. 使用 SmaliEnhancedMapper 推断未映射的方法/字段名
3. 使用 DeepEnhancer 从代码上下文推断
4. 合并所有推断结果，生成新的 mappings.txt
"""

import os
import re
import sys
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict

# 动态添加模块路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from process_java import parse_mapping
from smali_enhanced_deobf import (
    SmaliEnhancedMapper, SmaliClass, SmaliMethod,
    create_smali_mapper, load_smali_class, scan_all_smali_classes_parallel,
    CONFIG
)


# ==================== 配置 ====================

def get_config():
    project_root = os.path.dirname(SCRIPT_DIR)
    return {
        'MAPPING_FILE': os.path.join(project_root, 'mappings.txt'),
        'OUTPUT_FILE': os.path.join(project_root, 'mappings_enhanced.txt'),
        'SMALI_DIR': os.path.join(project_root, 'smali_output'),
        'INPUT_DIR': os.path.join(project_root, 'Processed_Classify_Merged'),
    }


LOCAL_CONFIG = get_config()


# ==================== 映射增强器 ====================

class MappingEnhancer:
    """映射增强器 - 生成增强的 mappings.txt"""
    
    def __init__(self, class_map: Dict[str, str], member_map: Dict[str, List[dict]]):
        self.class_map = class_map
        self.member_map = member_map
        self.mapper = create_smali_mapper(
            class_map, member_map,
            smali_dir=LOCAL_CONFIG['SMALI_DIR'],
            enable_heuristics=True,
            heuristic_prefix='auto_'
        )
        
        # 新增的映射
        self.new_method_mappings: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        self.new_field_mappings: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        
        # 统计
        self.stats = {
            'classes_analyzed': 0,
            'methods_analyzed': 0,
            'fields_analyzed': 0,
            'new_method_mappings': 0,
            'new_field_mappings': 0,
            'heuristic_methods': 0,
            'inherited_methods': 0,
        }
    
    def analyze_class(self, obf_class: str) -> None:
        """分析单个类，收集新的映射"""
        smali_class = self.mapper.get_smali_class(obf_class)
        if not smali_class:
            return
        
        self.stats['classes_analyzed'] += 1
        
        # 分析方法
        for method in smali_class.methods:
            if method.name in ('<init>', '<clinit>'):
                continue
            
            self.stats['methods_analyzed'] += 1
            
            # 检查是否已有映射
            existing = self._get_existing_method_mapping(obf_class, method.name, method.descriptor)
            if existing:
                continue
            
            # 尝试推断
            inferred = self.mapper.infer_method_name(obf_class, method)
            if inferred and inferred != method.name:
                # 确定推断来源
                if inferred.startswith('auto_'):
                    self.stats['heuristic_methods'] += 1
                    source = 'heuristic'
                else:
                    self.stats['inherited_methods'] += 1
                    source = 'inherited'
                
                self.new_method_mappings[obf_class].append((
                    method.name,
                    inferred,
                    f"# From {source}, sig: ({', '.join(method.param_types)}){method.return_type}"
                ))
                self.stats['new_method_mappings'] += 1
        
        # 分析字段
        for field_name, field_type in smali_class.fields:
            self.stats['fields_analyzed'] += 1
            
            # 检查是否已有映射
            existing = self._get_existing_field_mapping(obf_class, field_name)
            if existing:
                continue
            
            # 尝试推断
            inferred = self.mapper.infer_field_name(obf_class, field_name, field_type)
            if inferred and inferred != field_name:
                self.new_field_mappings[obf_class].append((
                    field_name,
                    inferred,
                    f"# Type: {field_type}"
                ))
                self.stats['new_field_mappings'] += 1
    
    def _get_existing_method_mapping(self, obf_class: str, obf_name: str, descriptor: str) -> Optional[str]:
        """检查是否已有方法映射"""
        if obf_class not in self.member_map:
            return None
        
        for m in self.member_map[obf_class]:
            if m.get('is_method') and m['obf'] == obf_name:
                # 如果有签名，需要匹配签名
                if m.get('descriptor') and m['descriptor'] == descriptor:
                    return m['orig']
                elif not m.get('descriptor'):
                    return m['orig']
        return None
    
    def _get_existing_field_mapping(self, obf_class: str, obf_name: str) -> Optional[str]:
        """检查是否已有字段映射"""
        if obf_class not in self.member_map:
            return None
        
        for m in self.member_map[obf_class]:
            if not m.get('is_method') and m['obf'] == obf_name:
                return m['orig']
        return None
    
    def analyze_all_classes(self) -> None:
        """分析所有类"""
        print(f"分析所有类...")
        
        for obf_class in self.class_map.keys():
            self.analyze_class(obf_class)
        
        print(f"  类分析数: {self.stats['classes_analyzed']}")
        print(f"  方法分析数: {self.stats['methods_analyzed']}")
        print(f"  字段分析数: {self.stats['fields_analyzed']}")
        print(f"  新增方法映射: {self.stats['new_method_mappings']}")
        print(f"  新增字段映射: {self.stats['new_field_mappings']}")
        print(f"    - 继承推断: {self.stats['inherited_methods']}")
        print(f"    - 启发式推断: {self.stats['heuristic_methods']}")
    
    def generate_enhanced_mapping(self, output_path: str) -> None:
        """生成增强的映射文件"""
        # 读取原始映射文件
        with open(LOCAL_CONFIG['MAPPING_FILE'], 'r', encoding='utf-8') as f:
            original_content = f.read()
        
        # 解析原始映射，按类分组
        lines = original_content.split('\n')
        
        # 构建增强映射
        enhanced_lines = []
        enhanced_lines.append("# Enhanced Mappings")
        enhanced_lines.append("# Generated by mapping_enhancer.py")
        enhanced_lines.append(f"# New method mappings: {self.stats['new_method_mappings']}")
        enhanced_lines.append(f"# New field mappings: {self.stats['new_field_mappings']}")
        enhanced_lines.append("")
        
        current_class = None
        class_block = []
        
        for line in lines:
            # 检测类定义
            class_match = re.match(r'^(\S+)\s*->\s*(\S+):$', line)
            
            if class_match:
                # 输出前一个类的内容（包括新增映射）
                if current_class and class_block:
                    enhanced_lines.extend(class_block)
                    
                    # 添加新增的方法映射
                    if current_class in self.new_method_mappings:
                        enhanced_lines.append("    # === NEW METHOD MAPPINGS ===")
                        for obf, orig, comment in self.new_method_mappings[current_class]:
                            enhanced_lines.append(f"    {obf}() -> {orig}  {comment}")
                    
                    # 添加新增的字段映射
                    if current_class in self.new_field_mappings:
                        enhanced_lines.append("    # === NEW FIELD MAPPINGS ===")
                        for obf, orig, comment in self.new_field_mappings[current_class]:
                            enhanced_lines.append(f"    {obf} -> {orig}  {comment}")
                
                current_class = class_match.group(1)
                class_block = [line]
            elif current_class:
                class_block.append(line)
            else:
                enhanced_lines.append(line)
        
        # 处理最后一个类
        if current_class and class_block:
            enhanced_lines.extend(class_block)
            
            if current_class in self.new_method_mappings:
                enhanced_lines.append("    # === NEW METHOD MAPPINGS ===")
                for obf, orig, comment in self.new_method_mappings[current_class]:
                    enhanced_lines.append(f"    {obf}() -> {orig}  {comment}")
            
            if current_class in self.new_field_mappings:
                enhanced_lines.append("    # === NEW FIELD MAPPINGS ===")
                for obf, orig, comment in self.new_field_mappings[current_class]:
                    enhanced_lines.append(f"    {obf} -> {orig}  {comment}")
        
        # 添加未在原映射中的类
        existing_classes = set()
        for line in lines:
            class_match = re.match(r'^(\S+)\s*->\s*(\S+):$', line)
            if class_match:
                existing_classes.add(class_match.group(1))
        
        new_class_mappings = []
        for obf_class in self.new_method_mappings.keys():
            if obf_class not in existing_classes:
                orig_class = self.class_map.get(obf_class, obf_class)
                new_class_mappings.append((obf_class, orig_class))
        
        for obf_class in self.new_field_mappings.keys():
            if obf_class not in existing_classes and obf_class not in [c[0] for c in new_class_mappings]:
                orig_class = self.class_map.get(obf_class, obf_class)
                new_class_mappings.append((obf_class, orig_class))
        
        if new_class_mappings:
            enhanced_lines.append("")
            enhanced_lines.append("# === NEW CLASS MAPPINGS ===")
            for obf_class, orig_class in new_class_mappings:
                enhanced_lines.append(f"{obf_class} -> {orig_class}:")
                
                if obf_class in self.new_method_mappings:
                    for obf, orig, comment in self.new_method_mappings[obf_class]:
                        enhanced_lines.append(f"    {obf}() -> {orig}  {comment}")
                
                if obf_class in self.new_field_mappings:
                    for obf, orig, comment in self.new_field_mappings[obf_class]:
                        enhanced_lines.append(f"    {obf} -> {orig}  {comment}")
        
        # 写入文件
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(enhanced_lines))
        
        print(f"\n增强映射已生成: {output_path}")


# ==================== 从代码推断的额外映射 ====================

class CodeBasedMappingExtractor:
    """从反编译代码中提取额外映射"""
    
    # 字符串键名模式
    STRING_KEY_PATTERNS = [
        # SharedPreferences
        (r'getString\s*\(\s*"([^"]+)"', 'pref'),
        (r'getInt\s*\(\s*"([^"]+)"', 'pref'),
        (r'getBoolean\s*\(\s*"([^"]+)"', 'pref'),
        (r'putString\s*\(\s*"([^"]+)"', 'pref'),
        
        # JSON
        (r'optString\s*\(\s*"([^"]+)"', 'json'),
        (r'optInt\s*\(\s*"([^"]+)"', 'json'),
        (r'getJSONObject\s*\(\s*"([^"]+)"', 'json'),
    ]
    
    # 字段赋值模式: this.field = xxx.getString("key", ...)
    FIELD_ASSIGNMENT_PATTERN = re.compile(
        r'this\.(\w+)\s*=\s*\w+\.getString\s*\(\s*"([^"]+)"'
    )
    
    @classmethod
    def extract_field_hints_from_code(cls, code: str) -> Dict[str, str]:
        """从代码中提取字段名线索"""
        hints = {}
        
        for match in cls.FIELD_ASSIGNMENT_PATTERN.finditer(code):
            field_name = match.group(1)
            key = match.group(2)
            
            # 只处理短名称（混淆的）
            if len(field_name) <= 2:
                suggested = cls._key_to_camel_case(key)
                hints[field_name] = suggested
        
        return hints
    
    @staticmethod
    def _key_to_camel_case(key: str) -> str:
        """将键名转换为驼峰命名"""
        if '_' in key:
            parts = key.lower().split('_')
            return parts[0] + ''.join(p.capitalize() for p in parts[1:])
        if key.isupper():
            return key.lower()
        return key[0].lower() + key[1:] if key else key


# ==================== 主函数 ====================

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='生成增强映射文件')
    parser.add_argument('--mapping-file', default=LOCAL_CONFIG['MAPPING_FILE'], help='原始映射文件')
    parser.add_argument('--output', default=LOCAL_CONFIG['OUTPUT_FILE'], help='输出映射文件')
    parser.add_argument('--smali-dir', default=LOCAL_CONFIG['SMALI_DIR'], help='Smali 目录')
    
    args = parser.parse_args()
    
    print("=== 增强映射生成器 ===\n")
    
    # 加载映射
    print("加载原始映射文件...")
    class_map, member_map = parse_mapping(args.mapping_file)
    print(f"  类映射数: {len(class_map)}")
    print(f"  成员映射类数: {len(member_map)}")
    
    # 创建增强器
    print("\n初始化映射增强器...")
    LOCAL_CONFIG['SMALI_DIR'] = args.smali_dir
    enhancer = MappingEnhancer(class_map, member_map)
    
    # 分析所有类
    print("\n开始分析...")
    enhancer.analyze_all_classes()
    
    # 生成增强映射
    print("\n生成增强映射...")
    enhancer.generate_enhanced_mapping(args.output)
    
    print("\n=== 完成 ===")


if __name__ == '__main__':
    main()
