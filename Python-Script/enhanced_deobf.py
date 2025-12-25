#!/usr/bin/env python3
"""
增强反混淆主脚本 (v2.0)

修复:
1. 括号计数器 - 使用状态机跳过字符串和注释
2. 方法正则 - 支持包级私有（无修饰符）方法
3. 路径配置 - 使用相对路径和环境变量
"""

import os
import re
import sys
import argparse
from typing import Dict, List, Tuple, Optional

# 动态添加模块路径 (相对于当前脚本)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from process_java import parse_mapping
from smali_enhanced_deobf import (
    SmaliEnhancedMapper, SmaliClass, SmaliMethod,
    create_smali_mapper, load_smali_class, scan_all_smali_classes_parallel,
    create_smali_enhancer
)

# XRef 分析器
try:
    from xref_analyzer import build_xref_index, CallGraphAnalyzer, XRefIndex
    XREF_AVAILABLE = True
except ImportError:
    XREF_AVAILABLE = False
    CallGraphAnalyzer = None
    XRefIndex = None


# ==================== 配置 (外部化) ====================

def get_config():
    """从环境变量或默认值获取配置"""
    # 基于脚本目录计算默认路径
    project_root = os.path.dirname(SCRIPT_DIR)
    
    return {
        'MAPPING_FILE': os.environ.get('MAPPING_FILE', os.path.join(project_root, 'mappings.txt')),
        'INPUT_DIR': os.environ.get('INPUT_DIR', os.path.join(project_root, 'Processed_Classify_Merged')),
        'OUTPUT_DIR': os.environ.get('OUTPUT_DIR', os.path.join(project_root, 'Enhanced_Deobf_Output')),
        'SMALI_DIR': os.environ.get('SMALI_DIR', os.path.join(project_root, 'smali_output')),
    }


LOCAL_CONFIG = get_config()


# ==================== Java 代码解析工具 ====================

class JavaCodeParser:
    """
    Java 代码解析工具 - 健壮的文本处理
    
    使用状态机正确处理:
    - 字符串字面量 ("...")
    - 字符字面量 ('.')
    - 单行注释 (// ...)
    - 多行注释 (/* ... */)
    """
    
    @staticmethod
    def extract_method_body(content: str, start_index: int) -> Tuple[str, int]:
        """
        从起始位置提取方法体
        
        Args:
            content: 完整代码内容
            start_index: 方法体开始位置 (第一个 '{' 之后)
        
        Returns:
            (方法体内容, 结束位置)
        """
        brace_count = 1
        i = start_index
        length = len(content)
        
        # 状态标志
        in_string = False      # 在字符串中
        in_char = False        # 在字符字面量中
        in_line_comment = False  # 在单行注释中
        in_block_comment = False  # 在多行注释中
        
        while i < length and brace_count > 0:
            char = content[i]
            prev = content[i - 1] if i > 0 else ''
            next_char = content[i + 1] if i + 1 < length else ''
            
            # 处理行尾 - 退出单行注释
            if char == '\n' and in_line_comment:
                in_line_comment = False
                i += 1
                continue
            
            # 跳过注释内容
            if in_line_comment or in_block_comment:
                # 检查多行注释结束
                if in_block_comment and char == '*' and next_char == '/':
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            
            # 处理转义字符
            if prev == '\\' and not (in_line_comment or in_block_comment):
                i += 1
                continue
            
            # 检查注释开始
            if not in_string and not in_char:
                if char == '/' and next_char == '/':
                    in_line_comment = True
                    i += 2
                    continue
                if char == '/' and next_char == '*':
                    in_block_comment = True
                    i += 2
                    continue
            
            # 处理字符串状态切换
            if char == '"' and not in_char:
                # 检查是否是转义的引号
                if prev != '\\':
                    in_string = not in_string
                i += 1
                continue
            
            # 处理字符字面量状态切换
            if char == "'" and not in_string:
                if prev != '\\':
                    in_char = not in_char
                i += 1
                continue
            
            # 仅在非字符串/字符/注释状态下计数括号
            if not in_string and not in_char:
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
            
            i += 1
        
        return content[start_index:i - 1], i
    
    @staticmethod
    def find_method_definitions(content: str) -> List[Tuple[int, int, str, str]]:
        """
        查找所有方法定义
        
        支持:
        - 有修饰符的方法 (public/private/protected)
        - 无修饰符的包级私有方法
        - 各种修饰符组合
        
        Returns:
            [(start, end, method_name, full_match), ...]
        """
        # 增强的方法正则 - 支持包级私有方法
        # (?:...)? 使修饰符可选
        pattern = re.compile(
            r'(?:^|\s)'
            r'((?:(?:public|private|protected)\s+)?'    # 访问修饰符 (可选)
            r'(?:static\s+)?'                           # static (可选)
            r'(?:final\s+)?'                            # final (可选)
            r'(?:abstract\s+)?'                         # abstract (可选)
            r'(?:synchronized\s+)?'                     # synchronized (可选)
            r'(?:native\s+)?'                           # native (可选)
            r'(?:strictfp\s+)?'                         # strictfp (可选)
            r'(?:<[\w\s,<>?]+>\s+)?'                    # 泛型返回类型 (可选)
            r'[\w\[\]<>?,\s]+\s+'                       # 返回类型
            r'(\w+)'                                    # 方法名
            r'\s*\([^)]*\)'                             # 参数列表
            r'(?:\s+throws\s+[\w\s,]+)?'                # throws 子句 (可选)
            r'\s*\{)',                                  # 方法体开始
            re.MULTILINE
        )
        
        results = []
        for match in pattern.finditer(content):
            full_match = match.group(1)
            method_name = match.group(2)
            results.append((match.start(1), match.end(1), method_name, full_match))
        
        return results
    
    @staticmethod
    def find_method_declarations(content: str) -> List[Tuple[int, int, str, str]]:
        """
        查找所有方法声明 (包括接口方法和抽象方法)
        
        Returns:
            [(start, end, method_name, full_match), ...]
        """
        # 方法声明正则 - 以分号结束
        pattern = re.compile(
            r'(?:^|\s)'
            r'((?:(?:public|private|protected)\s+)?'
            r'(?:static\s+)?'
            r'(?:abstract\s+)?'
            r'(?:default\s+)?'                          # 接口默认方法
            r'(?:native\s+)?'
            r'(?:<[\w\s,<>?]+>\s+)?'
            r'[\w\[\]<>?,\s]+\s+'
            r'(\w+)'
            r'\s*\([^)]*\)'
            r'(?:\s+throws\s+[\w\s,]+)?'
            r'\s*;)',
            re.MULTILINE
        )
        
        results = []
        for match in pattern.finditer(content):
            full_match = match.group(1)
            method_name = match.group(2)
            results.append((match.start(1), match.end(1), method_name, full_match))
        
        return results


# ==================== 增强处理器 ====================

class EnhancedDeobfuscator:
    """增强反混淆处理器 (v3.0 - 集成 DeepEnhancer)"""
    
    def __init__(
        self,
        class_map: Dict[str, str],
        member_map: Dict[str, List[dict]],
        smali_dir: str = None,
        enable_deep_enhance: bool = True,
        enable_xref: bool = False,
        full_smali_dir: str = None  # 完整 Smali 目录 (包含方法体)
    ):
        self.class_map = class_map
        self.member_map = member_map
        self.smali_dir = smali_dir or LOCAL_CONFIG['SMALI_DIR']
        self.mapper = create_smali_mapper(class_map, member_map, smali_dir=self.smali_dir)
        self.enhancer = create_smali_enhancer(self.mapper, class_map)
        self.parser = JavaCodeParser()
        
        # XRef 和调用图分析
        self.enable_xref = enable_xref and XREF_AVAILABLE
        self.xref_index = None
        self.call_graph = None
        
        if self.enable_xref and full_smali_dir:
            print("构建全局调用图...")
            self.xref_index, self.call_graph = build_xref_index(full_smali_dir)
        
        # 深度增强器 v3.0
        self.enable_deep_enhance = enable_deep_enhance
        self.deep_enhancer = DeepEnhancer(self.mapper, self.call_graph) if enable_deep_enhance else None
        
        # 统计
        self.stats = {
            'files_processed': 0,
            'classes_enhanced': 0,
            'methods_annotated': 0,
            'methods_inferred': 0
        }
    
    def process_file(self, input_path: str, output_path: str):
        """处理单个文件"""
        with open(input_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        
        if input_path.endswith('.txt'):
            enhanced = self._process_merged_file(content)
        else:
            enhanced = self._process_java_file(content)
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(enhanced)
        
        self.stats['files_processed'] += 1
    
    def _process_merged_file(self, content: str) -> str:
        """处理合并的分类文件"""
        lines = content.split('\n')
        result_lines = []
        
        current_file_content = []
        current_class = None
        in_file_section = False
        
        for line in lines:
            if line.startswith('File: ') and 'java' in line:
                if current_class and current_file_content:
                    enhanced = self._enhance_class_content(
                        '\n'.join(current_file_content),
                        current_class
                    )
                    result_lines.extend(enhanced.split('\n'))
                
                match = re.search(r'File: (.+\.java)', line)
                if match:
                    path = match.group(1)
                    current_class = path.replace('/', '.').replace('.java', '')
                    result_lines.append(line)
                    current_file_content = []
                    in_file_section = True
                    continue
            
            if in_file_section and line.startswith('---'):
                result_lines.append(line)
                continue
            
            if in_file_section:
                current_file_content.append(line)
            else:
                result_lines.append(line)
        
        if current_class and current_file_content:
            enhanced = self._enhance_class_content(
                '\n'.join(current_file_content),
                current_class
            )
            result_lines.extend(enhanced.split('\n'))
        
        return '\n'.join(result_lines)
    
    def _process_java_file(self, content: str) -> str:
        """处理单个 Java 文件"""
        pkg_match = re.search(r'package\s+([\w.]+);', content)
        class_match = re.search(
            r'(?:public\s+)?(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)',
            content
        )
        
        if pkg_match and class_match:
            full_class = f"{pkg_match.group(1)}.{class_match.group(1)}"
            return self._enhance_class_content(content, full_class)
        
        return content
    
    def _enhance_class_content(self, content: str, obf_class: str) -> str:
        """增强类内容"""
        smali_class = self.mapper.get_smali_class(obf_class)
        if not smali_class:
            return content
        
        self.stats['classes_enhanced'] += 1
        enhanced = content
        
        # 1. 为方法添加签名注释 - 使用增强正则
        for method in smali_class.methods:
            if method.name in ('<init>', '<clinit>'):
                continue
            
            # 增强的方法正则 - 支持包级私有方法
            method_pattern = re.compile(
                r'((?:(?:public|private|protected)\s+)?'   # 访问修饰符可选
                r'(?:static\s+)?(?:abstract\s+)?(?:final\s+)?(?:synchronized\s+)?'
                r'(?:native\s+)?'
                r'\S+\s+' + re.escape(method.name) + r'\s*\([^)]*\)\s*(?:throws\s+[^{;]*)?[{;])',
                re.MULTILINE
            )
            
            params_str = ', '.join(method.param_types) if method.param_types else ''
            
            inferred_name = self.mapper.infer_method_name(obf_class, method)
            if inferred_name and inferred_name != method.name:
                sig_comment = f"/* @SmaliSig: {method.return_type} {method.name}({params_str}) -> {inferred_name} */"
                self.stats['methods_inferred'] += 1
            else:
                sig_comment = f"/* @SmaliSig: {method.return_type} {method.name}({params_str}) */"
            
            def add_comment(m, sig=sig_comment):
                original = m.group(0)
                # 检查同一行是否已有注释
                line_start = enhanced.rfind('\n', 0, m.start()) + 1
                line_content = enhanced[line_start:m.start()]
                if '@SmaliSig' in line_content:
                    return original
                self.stats['methods_annotated'] += 1
                return f"{sig} {original}"
            
            enhanced = method_pattern.sub(add_comment, enhanced, count=1)
        
        # 2. 添加字段类型信息
        for field_name, field_type in smali_class.fields:
            field_pattern = re.compile(
                r'(\b' + re.escape(field_type) + r'\s+' + re.escape(field_name) + r'\s*[;=])',
                re.MULTILINE
            )
            
            inferred_field = self.mapper.infer_field_name(obf_class, field_name, field_type)
            if inferred_field and inferred_field != field_name:
                field_comment = f"/* @FieldHint: -> {inferred_field} */"
                enhanced = field_pattern.sub(f'{field_comment} \\1', enhanced, count=1)
        
        # 3. 添加继承信息注释
        if smali_class.super_class != 'java.lang.Object':
            super_comment = f"/* @Extends: {smali_class.super_class} */"
            enhanced = re.sub(
                r'(class\s+\w+)',
                f'\\1 {super_comment}',
                enhanced,
                count=1
            )
        
        if smali_class.interfaces:
            impl_comment = f"/* @Implements: {', '.join(smali_class.interfaces)} */"
            enhanced = re.sub(
                r'(class\s+\w+[^{]*)\{',
                f'\\1 {impl_comment} {{',
                enhanced,
                count=1
            )
        
        # 4. 应用深度增强 (v3.0)
        if self.deep_enhancer:
            enhanced = self.deep_enhancer.enhance_with_context(enhanced, obf_class)
        
        return enhanced
    
    def process_directory(self, input_dir: str = None, output_dir: str = None):
        """处理整个目录"""
        input_dir = input_dir or LOCAL_CONFIG['INPUT_DIR']
        output_dir = output_dir or LOCAL_CONFIG['OUTPUT_DIR']
        
        print(f"开始增强处理...")
        print(f"  输入目录: {input_dir}")
        print(f"  输出目录: {output_dir}")
        
        for root, dirs, files in os.walk(input_dir):
            for file in files:
                if file.endswith('.txt') or file.endswith('.java'):
                    input_path = os.path.join(root, file)
                    rel_path = os.path.relpath(input_path, input_dir)
                    output_path = os.path.join(output_dir, rel_path)
                    
                    print(f"  处理: {rel_path}")
                    self.process_file(input_path, output_path)
        
        print(f"\n=== 增强处理完成 ===")
        print(f"  处理文件数: {self.stats['files_processed']}")
        print(f"  增强类数: {self.stats['classes_enhanced']}")
        print(f"  Smali注释方法数: {self.stats['methods_annotated']}")
        print(f"  Smali推断方法名数: {self.stats['methods_inferred']}")
        
        # 深度增强统计
        if self.deep_enhancer:
            deep_stats = self.deep_enhancer.get_stats()
            print(f"\n=== 深度增强统计 (v3.0) ===")
            print(f"  方法分析数: {deep_stats['methods_analyzed']}")
            print(f"  深度推断方法数: {deep_stats['methods_inferred']}")
            print(f"  字段推断数: {deep_stats['fields_inferred']}")
            print(f"  字符串线索数: {deep_stats['string_hints_found']}")


# ==================== 深度增强器 (v3.0) ====================

class StringTracer:
    """
    字符串常量追踪器
    
    从代码中的字符串常量提取字段/方法名线索:
    - getString("key") / putString("key", value)
    - Log.d("TAG", "message about field")
    - equals("constantValue")
    - getIntent().getStringExtra("key")
    """
    
    # 常见的字符串提取模式
    STRING_PATTERNS = [
        # SharedPreferences
        (r'getString\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'getInt\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'getBoolean\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'getLong\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'getFloat\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'putString\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'putInt\s*\(\s*"([^"]+)"', 'pref_key'),
        (r'putBoolean\s*\(\s*"([^"]+)"', 'pref_key'),
        
        # Intent extras
        (r'getStringExtra\s*\(\s*"([^"]+)"', 'intent_extra'),
        (r'getIntExtra\s*\(\s*"([^"]+)"', 'intent_extra'),
        (r'getBooleanExtra\s*\(\s*"([^"]+)"', 'intent_extra'),
        (r'putExtra\s*\(\s*"([^"]+)"', 'intent_extra'),
        
        # Bundle
        (r'bundle\.getString\s*\(\s*"([^"]+)"', 'bundle_key'),
        (r'bundle\.getInt\s*\(\s*"([^"]+)"', 'bundle_key'),
        
        # JSON
        (r'getString\s*\(\s*"([^"]+)"', 'json_key'),
        (r'getJSONObject\s*\(\s*"([^"]+)"', 'json_key'),
        (r'getJSONArray\s*\(\s*"([^"]+)"', 'json_key'),
        (r'optString\s*\(\s*"([^"]+)"', 'json_key'),
        (r'optInt\s*\(\s*"([^"]+)"', 'json_key'),
        (r'put\s*\(\s*"([^"]+)"', 'json_key'),
        
        # 日志 (提取 TAG 和字段名)
        (r'Log\.[dievw]\s*\(\s*"([^"]+)"', 'log_tag'),
        (r'Log\.[dievw]\s*\([^,]+,\s*"([^"]*\s+(\w+)\s*[=:])', 'log_field'),
        
        # equals 比较
        (r'\.equals\s*\(\s*"([^"]+)"', 'equals_const'),
        
        # 资源引用
        (r'R\.string\.(\w+)', 'resource_string'),
        (r'R\.id\.(\w+)', 'resource_id'),
    ]
    
    @classmethod
    def extract_string_hints(cls, code: str) -> Dict[str, List[str]]:
        """
        从代码中提取字符串常量线索
        
        Returns:
            {hint_type: [value1, value2, ...]}
        """
        hints = {}
        
        for pattern, hint_type in cls.STRING_PATTERNS:
            matches = re.findall(pattern, code)
            if matches:
                if hint_type not in hints:
                    hints[hint_type] = []
                for match in matches:
                    if isinstance(match, tuple):
                        hints[hint_type].extend(match)
                    else:
                        hints[hint_type].append(match)
        
        return hints
    
    @classmethod
    def infer_field_name_from_key(cls, key: str) -> str:
        """
        从键名推断字段名
        
        例如: "player_name" -> "playerName"
              "PLAYER_NAME" -> "playerName"
        """
        # 处理下划线命名
        if '_' in key:
            parts = key.lower().split('_')
            return parts[0] + ''.join(p.capitalize() for p in parts[1:])
        
        # 处理全大写
        if key.isupper():
            return key.lower()
        
        # 已经是驼峰或其他格式
        return key[0].lower() + key[1:] if key else key
    
    @classmethod
    def find_field_assignments_with_strings(cls, code: str) -> List[Tuple[str, str, str]]:
        """
        查找字段赋值中使用的字符串
        
        例如: this.a = prefs.getString("player_name", "")
        返回: [('a', 'player_name', 'playerName')]
        """
        results = []
        
        # 模式: this.field = xxx.getString("key", ...)
        pattern = r'this\.(\w+)\s*=\s*\w+\.getString\s*\(\s*"([^"]+)"'
        for match in re.finditer(pattern, code):
            field_name = match.group(1)
            key = match.group(2)
            suggested = cls.infer_field_name_from_key(key)
            results.append((field_name, key, suggested))
        
        # 模式: this.field = xxx.getStringExtra("key")
        pattern = r'this\.(\w+)\s*=\s*.*?\.getStringExtra\s*\(\s*"([^"]+)"'
        for match in re.finditer(pattern, code):
            field_name = match.group(1)
            key = match.group(2)
            suggested = cls.infer_field_name_from_key(key)
            results.append((field_name, key, suggested))
        
        return results


class DeepEnhancer:
    """
    深度增强器 v3.0 - 综合上下文分析
    
    新增能力:
    1. 字符串常量追踪 (String Tracer)
    2. 扩展启发式规则 (集合操作 + 控制流模式)
    3. 字段名推断
    """
    
    # ========== 方法模式 (基于返回类型和参数数量) ==========
    METHOD_PATTERNS = {
        # boolean 返回类型
        ('boolean', 0): [
            (r'return\s+this\.(\w+)\s*;', 'is{0}'),  # return this.field -> isField
            (r'return\s+(\w+)\s*!=\s*null', 'has{0}'),  # return x != null -> hasX
            (r'return\s+(\w+)\s*>\s*0', 'is{0}Positive'),
            (r'return\s+(\w+)\s*<\s*0', 'is{0}Negative'),
            (r'return\s+(\w+)\s*==\s*0', 'is{0}Zero'),
            (r'return\s+true\s*;', 'isAlwaysTrue'),
            (r'return\s+false\s*;', 'isAlwaysFalse'),
            (r'return\s+this\.(\w+)\.isEmpty\s*\(\s*\)', 'is{0}Empty'),
            (r'return\s+this\.(\w+)\.contains\s*\(', 'contains'),
            (r'return\s+!this\.(\w+)', 'isNot{0}'),
        ],
        ('boolean', 1): [
            (r'return\s+this\.(\w+)\.equals\s*\(\s*\w+\s*\)', 'equals{0}'),
            (r'return\s+this\.(\w+)\.contains\s*\(\s*\w+\s*\)', 'contains{0}'),
            (r'return\s+\w+\s*==\s*this\.(\w+)', 'is{0}'),
            (r'return\s+\w+\s*instanceof\s+(\w+)', 'is{0}'),
            (r'\.equals\s*\(\s*\w+\s*\)', 'equals'),
            (r'\.contains\s*\(\s*\w+\s*\)', 'contains'),
            (r'\.startsWith\s*\(\s*\w+\s*\)', 'startsWith'),
            (r'\.endsWith\s*\(\s*\w+\s*\)', 'endsWith'),
        ],
        
        # void 返回类型
        ('void', 0): [
            (r'this\.(\w+)\.clear\s*\(\s*\)', 'clear{0}'),
            (r'this\.(\w+)\s*=\s*null\s*;', 'reset{0}'),
            (r'this\.(\w+)\s*=\s*0\s*;', 'reset{0}'),
            (r'this\.(\w+)\s*=\s*false\s*;', 'disable{0}'),
            (r'this\.(\w+)\s*=\s*true\s*;', 'enable{0}'),
            (r'notify\s*\(\s*\)', 'notifyChange'),
            (r'notifyAll\s*\(\s*\)', 'notifyAll'),
            (r'invalidate\s*\(\s*\)', 'invalidate'),
            (r'requestLayout\s*\(\s*\)', 'requestLayout'),
        ],
        ('void', 1): [
            (r'this\.(\w+)\s*=\s*(\w+)\s*;', 'set{0}'),  # this.field = param -> setField
            (r'this\.(\w+)\.add\s*\(\s*\w+\s*\)', 'addTo{0}'),
            (r'this\.(\w+)\.remove\s*\(\s*\w+\s*\)', 'removeFrom{0}'),
            (r'this\.(\w+)\.put\s*\(\s*\w+\s*,', 'putIn{0}'),
            (r'this\.(\w+)\.set\s*\(\s*\w+\s*\)', 'update{0}'),
            (r'this\.(\w+)\.addAll\s*\(\s*\w+\s*\)', 'addAllTo{0}'),
            (r'this\.(\w+)\.removeAll\s*\(\s*\w+\s*\)', 'removeAllFrom{0}'),
            (r'setVisibility\s*\(\s*\w+\s*\)', 'setVisibility'),
            (r'setEnabled\s*\(\s*\w+\s*\)', 'setEnabled'),
        ],
        ('void', 2): [
            (r'this\.(\w+)\.put\s*\(\s*\w+\s*,\s*\w+\s*\)', 'putIn{0}'),
            (r'this\.(\w+)\.set\s*\(\s*\w+\s*,\s*\w+\s*\)', 'setIn{0}'),
            (r'System\.arraycopy\s*\(', 'copyArray'),
        ],
        
        # int 返回类型
        ('int', 0): [
            (r'return\s+this\.(\w+)\.size\s*\(\s*\)', 'get{0}Count'),
            (r'return\s+this\.(\w+)\.length', 'get{0}Length'),
            (r'return\s+this\.(\w+)\s*;', 'get{0}'),
            (r'return\s+(\w+)\.hashCode\s*\(\s*\)', 'hashCode'),
            (r'return\s+0\s*;', 'getZero'),
            (r'return\s+-1\s*;', 'getInvalid'),
        ],
        ('int', 1): [
            (r'return\s+this\.(\w+)\.indexOf\s*\(\s*\w+\s*\)', 'indexOf{0}'),
            (r'return\s+(\w+)\.compareTo\s*\(\s*\w+\s*\)', 'compareTo'),
            (r'return\s+this\.(\w+)\.get\s*\(\s*\w+\s*\)', 'getFrom{0}'),
        ],
        
        # String 返回类型
        ('String', 0): [
            (r'return\s+this\.(\w+)\s*;', 'get{0}'),
            (r'return\s+"([^"]+)"\s*;', 'getConstant'),
            (r'return\s+this\.(\w+)\.toString\s*\(\s*\)', 'get{0}AsString'),
            (r'return\s+String\.valueOf\s*\(', 'toString'),
        ],
        
        # Object/集合返回类型
        ('Object', 0): [
            (r'return\s+this\.(\w+)\s*;', 'get{0}'),
            (r'return\s+new\s+(\w+)\s*\(', 'create{0}'),
            (r'return\s+this\.clone\s*\(\s*\)', 'clone'),
        ],
        ('Object', 1): [
            (r'return\s+this\.(\w+)\.get\s*\(\s*\w+\s*\)', 'getFrom{0}'),
            (r'return\s+this\.(\w+)\[', 'getFrom{0}'),
        ],
        
        # float/double 返回类型
        ('float', 0): [
            (r'return\s+this\.(\w+)\s*;', 'get{0}'),
            (r'return\s+this\.x\s*;', 'getX'),
            (r'return\s+this\.y\s*;', 'getY'),
            (r'return\s+this\.width\s*;', 'getWidth'),
            (r'return\s+this\.height\s*;', 'getHeight'),
        ],
    }
    
    # ========== 控制流模式 ==========
    CONTROL_FLOW_PATTERNS = [
        # 循环模式
        (r'for\s*\([^;]*;\s*\w+\s*<\s*this\.(\w+)\.size\s*\(\s*\)', 'iterate{0}'),
        (r'for\s*\(\s*\w+\s+\w+\s*:\s*this\.(\w+)\s*\)', 'forEach{0}'),
        (r'while\s*\(\s*\w+\.hasNext\s*\(\s*\)\s*\)', 'iterateCollection'),
        (r'while\s*\(\s*this\.(\w+)\s*>\s*0\s*\)', 'while{0}Positive'),
        
        # 条件模式
        (r'if\s*\(\s*this\.(\w+)\s*==\s*null\s*\)', 'checkNull{0}'),
        (r'if\s*\(\s*this\.(\w+)\s*!=\s*null\s*\)', 'ifHas{0}'),
        (r'if\s*\(\s*this\.(\w+)\.isEmpty\s*\(\s*\)\s*\)', 'ifEmpty{0}'),
        (r'if\s*\(\s*!this\.(\w+)\s*\)', 'ifNot{0}'),
        
        # 同步块
        (r'synchronized\s*\(\s*this\.(\w+)\s*\)', 'syncOn{0}'),
        (r'synchronized\s*\(\s*this\s*\)', 'syncOnSelf'),
        
        # 异常处理
        (r'try\s*\{[\s\S]*?catch\s*\(\s*(\w+Exception)', 'handle{0}'),
    ]
    
    # ========== 集合操作模式 ==========
    COLLECTION_PATTERNS = [
        # List 操作
        (r'\.add\s*\(\s*\d+\s*,', 'insertAt'),
        (r'\.addAll\s*\(', 'addAll'),
        (r'\.removeAll\s*\(', 'removeAll'),
        (r'\.retainAll\s*\(', 'retainAll'),
        (r'\.subList\s*\(', 'getSubList'),
        (r'\.toArray\s*\(', 'toArray'),
        
        # Map 操作
        (r'\.putAll\s*\(', 'putAll'),
        (r'\.keySet\s*\(', 'getKeys'),
        (r'\.values\s*\(', 'getValues'),
        (r'\.entrySet\s*\(', 'getEntries'),
        (r'\.containsKey\s*\(', 'containsKey'),
        (r'\.containsValue\s*\(', 'containsValue'),
        (r'\.getOrDefault\s*\(', 'getOrDefault'),
        
        # Set 操作
        (r'\.addAll\s*\(\s*Arrays\.asList', 'addMultiple'),
        (r'Collections\.sort\s*\(', 'sort'),
        (r'Collections\.reverse\s*\(', 'reverse'),
        (r'Collections\.shuffle\s*\(', 'shuffle'),
    ]
    
    def __init__(self, mapper: SmaliEnhancedMapper, call_graph: 'CallGraphAnalyzer' = None):
        self.mapper = mapper
        self.parser = JavaCodeParser()
        self.string_tracer = StringTracer()
        self.call_graph = call_graph  # 调用图分析器 (可选)
        
        # 统计信息
        self.stats = {
            'methods_analyzed': 0,
            'methods_inferred': 0,
            'fields_inferred': 0,
            'string_hints_found': 0,
            'xref_inferred': 0,
        }
    
    def infer_from_body(self, method_body: str, method: SmaliMethod) -> Optional[str]:
        """从方法体推断方法名 (v3.0)"""
        key = (method.return_type, len(method.param_types))
        
        # 1. 尝试基于返回类型的模式
        patterns = self.METHOD_PATTERNS.get(key, [])
        for pattern, name_template in patterns:
            match = re.search(pattern, method_body)
            if match:
                if '{0}' in name_template and match.groups():
                    # 替换模板中的占位符
                    field_name = match.group(1)
                    # 首字母大写
                    capitalized = field_name[0].upper() + field_name[1:] if field_name else ''
                    return name_template.format(capitalized)
                return name_template
        
        # 2. 尝试控制流模式
        for pattern, suggested_name in self.CONTROL_FLOW_PATTERNS:
            match = re.search(pattern, method_body)
            if match:
                if match.groups():
                    field_name = match.group(1)
                    capitalized = field_name[0].upper() + field_name[1:] if field_name else ''
                    return suggested_name.format(capitalized)
                return suggested_name
        
        # 3. 尝试集合操作模式
        for pattern, suggested_name in self.COLLECTION_PATTERNS:
            if re.search(pattern, method_body):
                return suggested_name
        
        # 4. 尝试从调用图推断
        if self.call_graph:
            xref_name = self._infer_from_call_graph(method)
            if xref_name:
                self.stats['xref_inferred'] += 1
                return xref_name
        
        return None
    
    def _infer_from_call_graph(self, method: SmaliMethod) -> Optional[str]:
        """
        从调用图推断方法名
        
        如果大多数调用者来自 draw/render 类方法，则目标方法可能也是绘制相关
        """
        if not self.call_graph:
            return None
        
        # 使用 CallGraphAnalyzer 的 infer_from_callers 方法
        # 注意: 需要将方法转换为正确的格式
        # 这里我们使用简化的推断逻辑
        return self.call_graph.infer_from_callers(
            'L' + method.name.replace('.', '/') + ';' if hasattr(method, 'class_name') else '',
            method.name,
            method.descriptor
        )
    
    def infer_field_names(self, code: str) -> Dict[str, str]:
        """
        从代码中推断字段名
        
        Returns:
            {obfuscated_name: suggested_name}
        """
        field_hints = {}
        
        # 1. 从字符串常量推断
        assignments = self.string_tracer.find_field_assignments_with_strings(code)
        for obf_name, key, suggested in assignments:
            if self._is_obfuscated_name(obf_name):
                field_hints[obf_name] = suggested
                self.stats['fields_inferred'] += 1
        
        # 2. 从类型推断常见字段名
        type_field_patterns = [
            (r'(PointF|Point)\s+(\w+)\s*;', ['position', 'location', 'point']),
            (r'(RectF|Rect)\s+(\w+)\s*;', ['bounds', 'rect', 'area']),
            (r'(Paint)\s+(\w+)\s*;', ['paint', 'brush']),
            (r'(Canvas)\s+(\w+)\s*;', ['canvas', 'surface']),
            (r'(Bitmap)\s+(\w+)\s*;', ['bitmap', 'image', 'texture']),
            (r'(Handler)\s+(\w+)\s*;', ['handler', 'mainHandler']),
            (r'(Context)\s+(\w+)\s*;', ['context', 'appContext']),
            (r'(Activity)\s+(\w+)\s*;', ['activity', 'parentActivity']),
        ]
        
        for pattern, suggestions in type_field_patterns:
            for match in re.finditer(pattern, code):
                field_name = match.group(2)
                if self._is_obfuscated_name(field_name):
                    field_hints[field_name] = suggestions[0]
                    self.stats['fields_inferred'] += 1
        
        return field_hints
    
    def _is_obfuscated_name(self, name: str) -> bool:
        """
        判断名称是否为混淆名称
        
        混淆名称模式:
        - 单字符: a, b, x, y
        - 短名称 (<=2): ab, x1
        - 小写字母+数字组合: a1, b2c, var1
        - 常见混淆模式: Field1, Var_1, a$1
        """
        if not name:
            return False
        
        # 单字符
        if len(name) == 1 and name.islower():
            return True
        
        # 短名称 (<=2)
        if len(name) <= 2:
            return True
        
        # 纯小写字母+数字组合 (如 a1, b2c, x1y2)
        if re.match(r'^[a-z][a-z0-9]*$', name) and len(name) <= 4:
            return True
        
        # 常见混淆模式: Field1, Var_1, val$1
        if re.match(r'^(Field|Var|val|var|arg|tmp)[_$]?\d+$', name, re.IGNORECASE):
            return True
        
        # 单字母+数字 (如 a1, b2, x3)
        if re.match(r'^[a-zA-Z]\d+$', name):
            return True
        
        return False
    
    def enhance_with_context(self, content: str, obf_class: str) -> str:
        """使用上下文分析增强代码 (v3.0)"""
        smali_class = self.mapper.get_smali_class(obf_class)
        if not smali_class:
            return content
        
        enhanced = content
        
        # 1. 提取字符串线索
        string_hints = self.string_tracer.extract_string_hints(content)
        if string_hints:
            self.stats['string_hints_found'] += sum(len(v) for v in string_hints.values())
        
        # 2. 推断字段名并添加注释
        field_hints = self.infer_field_names(content)
        for obf_name, suggested in field_hints.items():
            # 在字段声明处添加注释
            field_pattern = re.compile(
                r'(\b\w+\s+' + re.escape(obf_name) + r'\s*[;=])',
                re.MULTILINE
            )
            hint_comment = f"/* @FieldInferred: {obf_name} -> {suggested} */"
            enhanced = field_pattern.sub(f'{hint_comment} \\1', enhanced, count=1)
        
        # 3. 分析方法并添加注释
        offset_adjustment = 0
        method_defs = self.parser.find_method_definitions(content)
        
        for start, end, method_name, full_match in method_defs:
            self.stats['methods_analyzed'] += 1
            
            # 找到对应的 smali 方法
            smali_method = None
            for m in smali_class.methods:
                if m.name == method_name:
                    smali_method = m
                    break
            
            if not smali_method:
                continue
            
            # 提取并分析方法体
            method_body, body_end = self.parser.extract_method_body(content, end)
            
            inferred = self.infer_from_body(method_body, smali_method)
            if inferred:
                self.stats['methods_inferred'] += 1
                comment = f"/* @DeepInferred: {inferred} */"
                
                insert_pos = start + offset_adjustment
                enhanced = enhanced[:insert_pos] + comment + ' ' + enhanced[insert_pos:]
                offset_adjustment += len(comment) + 1
        
        # 4. 如果发现字符串线索，添加类级别注释
        if string_hints:
            class_hints = []
            if 'pref_key' in string_hints:
                class_hints.append(f"Prefs: {', '.join(string_hints['pref_key'][:5])}")
            if 'resource_id' in string_hints:
                class_hints.append(f"Views: {', '.join(string_hints['resource_id'][:5])}")
            if 'log_tag' in string_hints:
                class_hints.append(f"LogTags: {', '.join(set(string_hints['log_tag'][:3]))}")
            
            if class_hints:
                hint_block = f"/* @StringHints: {'; '.join(class_hints)} */\n"
                # 在 package 声明后插入
                pkg_match = re.search(r'(package\s+[\w.]+;)', enhanced)
                if pkg_match:
                    insert_pos = pkg_match.end()
                    enhanced = enhanced[:insert_pos] + '\n' + hint_block + enhanced[insert_pos:]
        
        return enhanced
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return self.stats.copy()


# ==================== 命令行接口 ====================

def main():
    parser = argparse.ArgumentParser(description='增强反混淆处理')
    parser.add_argument('--mapping-file', default=LOCAL_CONFIG['MAPPING_FILE'], help='映射文件路径')
    parser.add_argument('--input-dir', default=LOCAL_CONFIG['INPUT_DIR'], help='输入目录')
    parser.add_argument('--output-dir', default=LOCAL_CONFIG['OUTPUT_DIR'], help='输出目录')
    parser.add_argument('--smali-dir', default=LOCAL_CONFIG['SMALI_DIR'], help='Smali 目录 (存根文件)')
    parser.add_argument('--enable-xref', action='store_true', help='启用 XRef 调用图分析')
    parser.add_argument('--full-smali-dir', default=None, help='完整 Smali 目录 (包含方法体，用于 XRef)')
    
    args = parser.parse_args()
    
    print("=== 增强反混淆处理 ===\n")
    
    # 加载映射
    print("加载映射文件...")
    class_map, member_map = parse_mapping(args.mapping_file)
    print(f"  类映射数: {len(class_map)}")
    print(f"  成员映射类数: {len(member_map)}")
    
    # 创建增强处理器
    print("\n初始化增强处理器...")
    enhancer = EnhancedDeobfuscator(
        class_map, member_map,
        smali_dir=args.smali_dir,
        enable_xref=args.enable_xref,
        full_smali_dir=args.full_smali_dir
    )
    
    # 处理文件
    print("\n开始处理...")
    enhancer.process_directory(input_dir=args.input_dir, output_dir=args.output_dir)


if __name__ == '__main__':
    main()
