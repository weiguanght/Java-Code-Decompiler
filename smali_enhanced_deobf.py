#!/usr/bin/env python3
"""
Smali 增强反混淆模块 (v2.0)

修复内容:
1. 递归继承链查找 - 支持多层继承和接口
2. 增强 Smali 正则解析 - 处理 bridge/synthetic/constructor 等关键字
3. 描述符解析错误处理 - 非法描述符返回错误标记
4. 配置外部化 - 支持命令行参数和环境变量
5. 启发式策略可配置 - 支持返回猜测值并加前缀
6. 并行加载优化 - 使用 concurrent.futures 加速大规模扫描
"""

import os
import re
import argparse
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Android SDK 接口映射器
try:
    from android_interface_mapper import AndroidInterfaceMapper, create_android_mapper
    ANDROID_MAPPER_AVAILABLE = True
except ImportError:
    ANDROID_MAPPER_AVAILABLE = False


# ==================== 配置 ====================

def get_config():
    """从环境变量或默认值获取配置"""
    return {
        'SMALI_DIR': os.environ.get('SMALI_DIR', '/Users/hoto/PC_Java/smali_output'),
        'MAPPING_FILE': os.environ.get('MAPPING_FILE', '/Users/hoto/PC_Java/mappings.txt'),
        'OUTPUT_DIR': os.environ.get('OUTPUT_DIR', '/Users/hoto/PC_Java'),
        'ENABLE_HEURISTICS': os.environ.get('ENABLE_HEURISTICS', 'true').lower() == 'true',
        'HEURISTIC_PREFIX': os.environ.get('HEURISTIC_PREFIX', 'inferred_'),
        'MAX_WORKERS': int(os.environ.get('MAX_WORKERS', '8')),
    }


CONFIG = get_config()


# ==================== 数据结构 ====================

@dataclass
class SmaliMethod:
    """Smali 方法信息"""
    name: str
    descriptor: str
    return_type: str
    param_types: List[str]
    is_static: bool = False
    is_abstract: bool = False
    is_constructor: bool = False
    is_bridge: bool = False
    is_synthetic: bool = False


@dataclass
class SmaliClass:
    """Smali 类信息"""
    class_name: str
    super_class: str
    interfaces: List[str]
    methods: List[SmaliMethod]
    fields: List[Tuple[str, str]]  # (name, type)
    is_interface: bool = False
    is_abstract: bool = False


# ==================== JVM 类型解析 ====================

JVM_TYPE_MAP = {
    'V': 'void', 'Z': 'boolean', 'B': 'byte', 'C': 'char',
    'S': 'short', 'I': 'int', 'J': 'long', 'F': 'float', 'D': 'double'
}


def parse_jvm_type(desc: str) -> Tuple[str, bool]:
    """
    解析 JVM 类型描述符为 Java 类型
    
    Returns:
        (类型名, 是否解析成功)
    """
    if not desc:
        return 'void', True
    
    # 基本类型
    if desc in JVM_TYPE_MAP:
        return JVM_TYPE_MAP[desc], True
    
    # 数组类型
    if desc.startswith('['):
        element_type, success = parse_jvm_type(desc[1:])
        if not success:
            return f'<invalid_array:{desc}>', False
        return element_type + '[]', True
    
    # 对象类型 L...;
    if desc.startswith('L') and desc.endswith(';'):
        full_name = desc[1:-1].replace('/', '.')
        # 处理无包名的类 (如 La;)
        if '.' not in full_name:
            return full_name, True
        return full_name.split('.')[-1], True
    
    # 非法描述符
    return f'<invalid:{desc}>', False


def parse_method_descriptor(descriptor: str) -> Tuple[List[str], str, bool]:
    """
    解析方法描述符
    
    Returns:
        (参数类型列表, 返回类型, 是否解析成功)
    """
    if not descriptor or not descriptor.startswith('('):
        return [], 'void', False
    
    try:
        paren_end = descriptor.index(')')
    except ValueError:
        return [], '<invalid_descriptor>', False
    
    params_part = descriptor[1:paren_end]
    return_part = descriptor[paren_end + 1:]
    
    # 解析参数
    params = []
    i = 0
    parse_success = True
    
    while i < len(params_part):
        char = params_part[i]
        
        if char in JVM_TYPE_MAP:
            params.append(JVM_TYPE_MAP[char])
            i += 1
        elif char == 'L':
            try:
                end = params_part.index(';', i)
                type_name, success = parse_jvm_type(params_part[i:end + 1])
                if not success:
                    parse_success = False
                params.append(type_name)
                i = end + 1
            except ValueError:
                # 找不到分号，描述符非法
                params.append(f'<truncated:{params_part[i:]}>')
                parse_success = False
                break
        elif char == '[':
            # 数组类型
            array_depth = 0
            while i < len(params_part) and params_part[i] == '[':
                array_depth += 1
                i += 1
            if i >= len(params_part):
                params.append('<incomplete_array>')
                parse_success = False
                break
            
            if params_part[i] == 'L':
                try:
                    end = params_part.index(';', i)
                    base_type, success = parse_jvm_type(params_part[i:end + 1])
                    if not success:
                        parse_success = False
                    i = end + 1
                except ValueError:
                    base_type = f'<truncated:{params_part[i:]}>'
                    parse_success = False
                    break
            elif params_part[i] in JVM_TYPE_MAP:
                base_type = JVM_TYPE_MAP[params_part[i]]
                i += 1
            else:
                # 非法字符
                base_type = f'<invalid_base:{params_part[i]}>'
                parse_success = False
                i += 1
            params.append(base_type + '[]' * array_depth)
        else:
            # 非法字符 - 不在 JVM_TYPE_MAP 且不是 L 或 [
            params.append(f'<invalid_char:{char}>')
            parse_success = False
            i += 1
    
    # 解析返回类型
    return_type, ret_success = parse_jvm_type(return_part)
    if not ret_success:
        parse_success = False
    
    # 日志警告：非法描述符可能由混淆器故意插入
    if not parse_success:
        import sys
        print(f"Warning: Failed to parse method descriptor: {descriptor}", file=sys.stderr)
    
    return params, return_type, parse_success


# ==================== Smali 解析器 (增强版) ====================

# 方法修饰符模式
METHOD_MODIFIERS = r'(?:public|private|protected|static|final|abstract|synchronized|native|bridge|synthetic|varargs|strictfp|declared-synchronized)*'

# 方法声明正则 - 更宽松的匹配
METHOD_PATTERN = re.compile(
    r'\.method\s+(' + METHOD_MODIFIERS + r')\s*(\S+)\(([^)]*)\)(\S+)',
    re.IGNORECASE
)

# 备用方法正则 - 处理极端情况
METHOD_PATTERN_FALLBACK = re.compile(
    r'\.method\s+.*?([a-zA-Z_$<>][\w$<>]*)\(([^)]*)\)([^\s]+)',
    re.IGNORECASE
)

# 类声明正则
CLASS_PATTERN = re.compile(
    r'\.class\s+(' + METHOD_MODIFIERS + r')\s*(interface\s+)?(\S+)',
    re.IGNORECASE
)

# 字段正则 - 更宽松
FIELD_PATTERN = re.compile(
    r'\.field\s+(' + METHOD_MODIFIERS + r')\s*(\S+):(\S+)',
    re.IGNORECASE
)


def parse_smali_file(smali_path: str) -> Optional[SmaliClass]:
    """解析 smali 文件 (增强版)"""
    if not os.path.exists(smali_path):
        return None
    
    try:
        with open(smali_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception:
        return None
    
    class_name = ''
    super_class = 'java.lang.Object'
    interfaces = []
    methods = []
    fields = []
    is_interface = False
    is_abstract = False
    
    for line in content.split('\n'):
        line = line.strip()
        
        # 类声明
        if line.startswith('.class'):
            match = CLASS_PATTERN.search(line)
            if match:
                modifiers = match.group(1) or ''
                is_interface_kw = match.group(2)
                raw_name = match.group(3)
                
                # 解析类名 (处理 L...; 格式)
                if raw_name.startswith('L') and raw_name.endswith(';'):
                    class_name = raw_name[1:-1].replace('/', '.')
                else:
                    class_name = raw_name.replace('/', '.')
                
                is_interface = is_interface_kw is not None or 'interface' in modifiers
                is_abstract = 'abstract' in modifiers
        
        # 父类
        elif line.startswith('.super'):
            match = re.search(r'\.super\s+(\S+)', line)
            if match:
                raw_super = match.group(1)
                if raw_super.startswith('L') and raw_super.endswith(';'):
                    super_class = raw_super[1:-1].replace('/', '.')
                else:
                    super_class = raw_super.replace('/', '.')
        
        # 接口
        elif line.startswith('.implements'):
            match = re.search(r'\.implements\s+(\S+)', line)
            if match:
                raw_iface = match.group(1)
                if raw_iface.startswith('L') and raw_iface.endswith(';'):
                    interfaces.append(raw_iface[1:-1].replace('/', '.'))
                else:
                    interfaces.append(raw_iface.replace('/', '.'))
        
        # 方法
        elif line.startswith('.method'):
            match = METHOD_PATTERN.search(line)
            if not match:
                match = METHOD_PATTERN_FALLBACK.search(line)
            
            if match:
                if len(match.groups()) == 4:
                    modifiers = match.group(1) or ''
                    name = match.group(2)
                    params_desc = match.group(3)
                    return_desc = match.group(4)
                else:
                    modifiers = ''
                    name = match.group(1)
                    params_desc = match.group(2)
                    return_desc = match.group(3)
                
                descriptor = f'({params_desc}){return_desc}'
                params, return_type, _ = parse_method_descriptor(descriptor)
                
                methods.append(SmaliMethod(
                    name=name,
                    descriptor=descriptor,
                    return_type=return_type,
                    param_types=params,
                    is_static='static' in modifiers.lower() if modifiers else False,
                    is_abstract='abstract' in modifiers.lower() if modifiers else False,
                    is_constructor=name in ('<init>', '<clinit>'),
                    is_bridge='bridge' in modifiers.lower() if modifiers else False,
                    is_synthetic='synthetic' in modifiers.lower() if modifiers else False
                ))
        
        # 字段
        elif line.startswith('.field'):
            match = FIELD_PATTERN.search(line)
            if match:
                field_name = match.group(2)
                raw_type = match.group(3)
                # 处理默认值 (= xxx)
                if '=' in raw_type:
                    raw_type = raw_type.split('=')[0].strip()
                field_type, _ = parse_jvm_type(raw_type)
                fields.append((field_name, field_type))
    
    if not class_name:
        return None
    
    return SmaliClass(
        class_name=class_name,
        super_class=super_class,
        interfaces=interfaces,
        methods=methods,
        fields=fields,
        is_interface=is_interface,
        is_abstract=is_abstract
    )


def load_smali_class(obf_class_name: str, smali_dir: str = None) -> Optional[SmaliClass]:
    """从 smali_output 加载类信息"""
    if smali_dir is None:
        smali_dir = CONFIG['SMALI_DIR']
    
    smali_path = os.path.join(
        smali_dir,
        obf_class_name.replace('.', '/') + '.smali'
    )
    return parse_smali_file(smali_path)


# ==================== 启发式命名规则 ====================

RETURN_TYPE_METHOD_HINTS = {
    'boolean': ['is', 'has', 'can', 'should', 'check'],
    'int': ['get', 'count', 'size', 'index', 'calculate'],
    'long': ['get', 'getId', 'getTime', 'getTimestamp'],
    'float': ['get', 'getX', 'getY', 'getWidth', 'getHeight', 'calculate'],
    'double': ['get', 'calculate', 'compute'],
    'String': ['get', 'getName', 'toString', 'getDescription', 'format'],
    'void': ['set', 'update', 'init', 'reset', 'clear', 'add', 'remove', 'process'],
}

COMMON_METHOD_PATTERNS = {
    ('void', 0): ['init', 'update', 'reset', 'clear', 'dispose', 'destroy'],
    ('void', 1): ['set', 'add', 'remove', 'process', 'handle', 'apply'],
    ('void', 2): ['set', 'move', 'copy', 'swap', 'transfer'],
    ('boolean', 0): ['isValid', 'isEnabled', 'isEmpty', 'hasContent', 'isReady'],
    ('boolean', 1): ['equals', 'contains', 'matches', 'accept', 'canHandle'],
    ('int', 0): ['size', 'count', 'length', 'hashCode', 'getId'],
    ('int', 1): ['indexOf', 'compare', 'get', 'computeHash'],
    ('String', 0): ['toString', 'getName', 'getDescription', 'getValue'],
    ('Object', 0): ['get', 'create', 'clone', 'copy'],
    ('Object', 1): ['get', 'find', 'create', 'transform'],
}

# 特殊方法名模式
SPECIAL_METHOD_PATTERNS = {
    '<init>': '__constructor__',
    '<clinit>': '__static_init__',
}


# ==================== 增强型方法映射器 ====================

class SmaliEnhancedMapper:
    """
    基于 Smali 信息的增强型映射器 (v2.0)
    
    修复:
    1. 递归继承链查找
    2. 接口继承链支持
    3. 可配置的启发式策略
    """
    
    def __init__(
        self,
        class_map: Dict[str, str],
        member_map: Dict[str, List[dict]],
        smali_dir: str = None,
        enable_heuristics: bool = None,
        heuristic_prefix: str = None
    ):
        self.class_map = class_map
        self.member_map = member_map
        self.smali_dir = smali_dir or CONFIG['SMALI_DIR']
        self.enable_heuristics = enable_heuristics if enable_heuristics is not None else CONFIG['ENABLE_HEURISTICS']
        self.heuristic_prefix = heuristic_prefix or CONFIG['HEURISTIC_PREFIX']
        
        # 缓存
        self._smali_cache: Dict[str, Optional[SmaliClass]] = {}
        
        # 方法签名索引
        self._method_by_signature: Dict[Tuple[str, int], List[Tuple[str, str, str]]] = defaultdict(list)
        
        # 继承链缓存 (避免重复计算)
        self._inheritance_chain_cache: Dict[str, List[str]] = {}
        
        # Android SDK 接口映射器
        self._android_mapper = create_android_mapper() if ANDROID_MAPPER_AVAILABLE else None
        
        # 构建索引
        self._build_indices()
    
    def _build_indices(self):
        """构建方法签名索引"""
        for obf_class, members in self.member_map.items():
            for m in members:
                if m.get('is_method'):
                    sig = m.get('signature', '')
                    if sig:
                        param_count = sig.count(',') + 1 if sig.strip('()') else 0
                    else:
                        param_count = 0
                    
                    ret_type = m.get('return_type', 'void')
                    key = (ret_type, param_count)
                    self._method_by_signature[key].append((obf_class, m['obf'], m['orig']))
    
    def get_smali_class(self, obf_class_name: str) -> Optional[SmaliClass]:
        """获取 smali 类信息（带缓存）"""
        if obf_class_name not in self._smali_cache:
            self._smali_cache[obf_class_name] = load_smali_class(obf_class_name, self.smali_dir)
        return self._smali_cache[obf_class_name]
    
    def get_inheritance_chain(self, obf_class: str) -> List[str]:
        """
        获取完整继承链 (包括所有父类和接口)
        
        使用缓存避免重复计算
        """
        if obf_class in self._inheritance_chain_cache:
            return self._inheritance_chain_cache[obf_class]
        
        chain = []
        visited = set()
        queue = [obf_class]
        
        while queue:
            current = queue.pop(0)
            
            if current in visited or current == 'java.lang.Object' or current.startswith('java.'):
                continue
            visited.add(current)
            
            if current != obf_class:
                chain.append(current)
            
            smali_class = self.get_smali_class(current)
            if not smali_class:
                continue
            
            # 添加父类
            if smali_class.super_class and smali_class.super_class != 'java.lang.Object':
                queue.append(smali_class.super_class)
            
            # 添加接口
            for interface in smali_class.interfaces:
                queue.append(interface)
        
        self._inheritance_chain_cache[obf_class] = chain
        return chain
    
    def infer_method_name(self, obf_class: str, method: SmaliMethod) -> Optional[str]:
        """
        推断方法的语义名称
        
        优先级:
        1. 现有映射 (当前类) - 严格签名匹配
        2. 父类/接口中的同签名方法 (递归查找)
        3. 启发式规则 (可配置)
        
        修复: 处理方法重载歧义 - 当存在多个同名方法时，仅使用签名匹配
        """
        # 0. 特殊方法
        if method.name in SPECIAL_METHOD_PATTERNS:
            return SPECIAL_METHOD_PATTERNS[method.name]
        
        # 1. 检查当前类的现有映射
        if obf_class in self.member_map:
            # 收集所有同名方法
            same_name_methods = [
                m for m in self.member_map[obf_class]
                if m.get('is_method') and m['obf'] == method.name
            ]
            
            if len(same_name_methods) == 1:
                # 只有一个同名方法，可以安全匹配
                m = same_name_methods[0]
                # 但仍需验证参数数量（如果有信息）
                if m.get('descriptor'):
                    if m['descriptor'] == method.descriptor:
                        return m['orig']
                    # 签名不匹配，跳过
                else:
                    # 无签名信息，使用弱匹配但检查参数数量
                    if self._check_param_count_compatible(m, method):
                        return m['orig']
            elif len(same_name_methods) > 1:
                # 存在重载，必须严格匹配签名
                for m in same_name_methods:
                    if m.get('descriptor') and m['descriptor'] == method.descriptor:
                        return m['orig']
                # 无法精确匹配，跳过以避免歧义
        
        # 2. 递归查找继承链
        inherited_name = self._find_inherited_method_recursive(obf_class, method)
        if inherited_name:
            return inherited_name
        
        # 2.5 Android SDK 接口映射
        interface_name = self._infer_from_android_interface(obf_class, method)
        if interface_name:
            return interface_name
        
        # 3. 启发式规则
        if self.enable_heuristics:
            heuristic_name = self._apply_heuristics(method)
            if heuristic_name:
                return f"{self.heuristic_prefix}{heuristic_name}"
        
        return None
    
    def _infer_from_android_interface(self, obf_class: str, method: SmaliMethod) -> Optional[str]:
        """
        从 Android SDK 接口映射推断方法名
        
        如果类实现了标准 Android/Java 接口，根据方法签名匹配接口方法
        """
        if not self._android_mapper:
            return None
        
        smali_class = self.get_smali_class(obf_class)
        if not smali_class or not smali_class.interfaces:
            return None
        
        result = self._android_mapper.get_method_name_by_interface(
            smali_class.interfaces,
            method.descriptor
        )
        
        if result:
            interface, method_name = result
            return method_name
        
        return None
    
    def _check_param_count_compatible(self, mapping: dict, method: SmaliMethod) -> bool:
        """
        检查映射条目与方法的参数数量是否兼容
        
        用于在无签名信息时进行最低限度的验证
        """
        # 从 signature 字段提取参数数量
        sig = mapping.get('signature', '')
        if not sig:
            # 无签名信息，假设兼容（保守策略）
            return True
        
        # 解析 signature 中的参数数量
        # 格式通常为 "(Type1, Type2)" 或 "()"
        sig = sig.strip()
        if sig.startswith('(') and ')' in sig:
            params_part = sig[1:sig.index(')')]
            if not params_part.strip():
                expected_count = 0
            else:
                expected_count = params_part.count(',') + 1
        else:
            return True  # 无法解析，假设兼容
        
        return expected_count == len(method.param_types)
    
    def _find_inherited_method_recursive(self, obf_class: str, method: SmaliMethod) -> Optional[str]:
        """
        递归查找继承链中的同签名方法 (修复版)
        
        查找顺序:
        1. 严格签名匹配 (最安全)
        2. 名称匹配 + 参数数量验证 (仅当无重载时)
        
        修复: 处理重载歧义，避免错误匹配
        """
        inheritance_chain = self.get_inheritance_chain(obf_class)
        
        for ancestor in inheritance_chain:
            if ancestor not in self.member_map:
                continue
            
            # 第一遍：严格签名匹配
            for m in self.member_map[ancestor]:
                if not m.get('is_method'):
                    continue
                
                if m.get('descriptor') and m['descriptor'] == method.descriptor:
                    return m['orig']
            
            # 第二遍：名称匹配（仅当该祖先类中无重载时）
            same_name_methods = [
                m for m in self.member_map[ancestor]
                if m.get('is_method') and m['obf'] == method.name
            ]
            
            if len(same_name_methods) == 1:
                m = same_name_methods[0]
                # 验证参数数量兼容性
                if self._check_param_count_compatible(m, method):
                    return m['orig']
            # 如果存在多个同名方法（重载），跳过以避免歧义
        
        # 第三遍：通过 Smali 文件查找同签名方法
        for ancestor in inheritance_chain:
            ancestor_smali = self.get_smali_class(ancestor)
            if not ancestor_smali:
                continue
            
            for ancestor_method in ancestor_smali.methods:
                if ancestor_method.descriptor == method.descriptor:
                    # 在映射表中查找该祖先方法的映射
                    if ancestor in self.member_map:
                        for m in self.member_map[ancestor]:
                            if m.get('is_method') and m['obf'] == ancestor_method.name:
                                # 再次验证签名（如果有）
                                if m.get('descriptor'):
                                    if m['descriptor'] == method.descriptor:
                                        return m['orig']
                                else:
                                    return m['orig']
        
        return None
    
    def _apply_heuristics(self, method: SmaliMethod) -> Optional[str]:
        """应用启发式规则推断方法名"""
        # 跳过构造函数和桥接方法
        if method.is_constructor or method.is_bridge or method.is_synthetic:
            return None
        
        ret_type = method.return_type
        param_count = len(method.param_types)
        
        # 基于常见模式
        key = (ret_type, param_count)
        if key in COMMON_METHOD_PATTERNS:
            candidates = COMMON_METHOD_PATTERNS[key]
            if candidates:
                # 返回第一个候选
                return candidates[0]
        
        # 基于返回类型
        if ret_type in RETURN_TYPE_METHOD_HINTS:
            hints = RETURN_TYPE_METHOD_HINTS[ret_type]
            if hints:
                return hints[0]
        
        return None
    
    def infer_field_name(self, obf_class: str, field_name: str, field_type: str) -> Optional[str]:
        """推断字段的语义名称"""
        # 检查现有映射
        if obf_class in self.member_map:
            for m in self.member_map[obf_class]:
                if not m.get('is_method') and m['obf'] == field_name:
                    return m['orig']
        
        # 递归检查父类
        inheritance_chain = self.get_inheritance_chain(obf_class)
        for ancestor in inheritance_chain:
            if ancestor in self.member_map:
                for m in self.member_map[ancestor]:
                    if not m.get('is_method') and m['obf'] == field_name:
                        return m['orig']
        
        return None
    
    def get_class_method_signatures(self, obf_class: str) -> List[Tuple[str, str, List[str]]]:
        """获取类的所有方法签名"""
        smali_class = self.get_smali_class(obf_class)
        if not smali_class:
            return []
        
        return [
            (m.name, m.return_type, m.param_types)
            for m in smali_class.methods
        ]


# ==================== 代码增强器 ====================

class SmaliCodeEnhancer:
    """使用 Smali 信息增强反编译代码"""
    
    def __init__(self, mapper: SmaliEnhancedMapper, class_map: Dict[str, str]):
        self.mapper = mapper
        self.class_map = class_map
    
    def enhance_code(self, code: str, obf_class: str) -> str:
        """增强反编译代码"""
        enhanced = code
        
        smali_class = self.mapper.get_smali_class(obf_class)
        if not smali_class:
            return enhanced
        
        # 为每个方法添加签名注释
        for method in smali_class.methods:
            if method.is_constructor:
                continue
            
            pattern = re.compile(
                r'((?:public|private|protected)\s+(?:static\s+)?(?:abstract\s+)?(?:final\s+)?'
                r'\S+\s+' + re.escape(method.name) + r'\s*\([^)]*\))',
                re.MULTILINE
            )
            
            inferred = self.mapper.infer_method_name(obf_class, method)
            params_str = ', '.join(method.param_types) if method.param_types else ''
            
            if inferred and inferred != method.name:
                sig_comment = f"/* @SmaliSig: {method.return_type} {method.name}({params_str}) -> {inferred} */"
            else:
                sig_comment = f"/* @SmaliSig: {method.return_type} {method.name}({params_str}) */"
            
            def add_comment(m):
                original = m.group(0)
                if '@SmaliSig' in code[:m.start()].split('\n')[-1]:
                    return original
                return f"{sig_comment} {original}"
            
            enhanced = pattern.sub(add_comment, enhanced, count=1)
        
        return enhanced


# ==================== 并行批量处理 ====================

def scan_all_smali_classes_parallel(smali_dir: str = None, max_workers: int = None) -> Dict[str, SmaliClass]:
    """并行扫描所有 smali 类"""
    if smali_dir is None:
        smali_dir = CONFIG['SMALI_DIR']
    if max_workers is None:
        max_workers = CONFIG['MAX_WORKERS']
    
    # 收集所有 smali 文件路径
    smali_files = []
    for root, dirs, files in os.walk(smali_dir):
        for file in files:
            if file.endswith('.smali'):
                smali_files.append(os.path.join(root, file))
    
    classes = {}
    
    def parse_file(path):
        return parse_smali_file(path)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {executor.submit(parse_file, path): path for path in smali_files}
        
        for future in as_completed(future_to_path):
            smali_class = future.result()
            if smali_class and smali_class.class_name:
                classes[smali_class.class_name] = smali_class
    
    return classes


def scan_all_smali_classes(smali_dir: str = None) -> Dict[str, SmaliClass]:
    """扫描所有 smali 类 (串行版本，用于小规模)"""
    if smali_dir is None:
        smali_dir = CONFIG['SMALI_DIR']
    
    classes = {}
    
    for root, dirs, files in os.walk(smali_dir):
        for file in files:
            if file.endswith('.smali'):
                smali_path = os.path.join(root, file)
                smali_class = parse_smali_file(smali_path)
                if smali_class and smali_class.class_name:
                    classes[smali_class.class_name] = smali_class
    
    return classes


def generate_unmapped_method_report(
    class_map: Dict[str, str],
    member_map: Dict[str, List[dict]],
    output_path: str,
    smali_dir: str = None
):
    """生成未映射方法报告"""
    mapper = SmaliEnhancedMapper(class_map, member_map, smali_dir=smali_dir, enable_heuristics=False)
    
    unmapped_methods = []
    total_methods = 0
    mapped_methods = 0
    heuristic_methods = 0
    
    for obf_class in class_map.keys():
        smali_class = mapper.get_smali_class(obf_class)
        if not smali_class:
            continue
        
        for method in smali_class.methods:
            if method.is_constructor:
                continue
            
            total_methods += 1
            
            # 使用带启发式的 mapper 检查
            mapper_with_heuristics = SmaliEnhancedMapper(
                class_map, member_map, smali_dir=smali_dir, enable_heuristics=True
            )
            inferred = mapper_with_heuristics.infer_method_name(obf_class, method)
            
            if inferred:
                if inferred.startswith(CONFIG['HEURISTIC_PREFIX']):
                    heuristic_methods += 1
                else:
                    mapped_methods += 1
            else:
                unmapped_methods.append({
                    'class': obf_class,
                    'method': method.name,
                    'return_type': method.return_type,
                    'param_types': method.param_types,
                    'descriptor': method.descriptor
                })
    
    # 按类分组
    by_class = defaultdict(list)
    for m in unmapped_methods:
        by_class[m['class']].append(m)
    
    # 生成报告
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("# 未映射方法报告\n")
        f.write(f"# 总方法数: {total_methods}\n")
        f.write(f"# 精确映射: {mapped_methods} ({mapped_methods/max(total_methods,1)*100:.1f}%)\n")
        f.write(f"# 启发推断: {heuristic_methods} ({heuristic_methods/max(total_methods,1)*100:.1f}%)\n")
        f.write(f"# 未映射: {len(unmapped_methods)} ({len(unmapped_methods)/max(total_methods,1)*100:.1f}%)\n\n")
        
        for cls in sorted(by_class.keys()):
            methods = by_class[cls]
            f.write(f"=== {cls} ===\n")
            for m in methods:
                params = ', '.join(m['param_types'])
                f.write(f"  {m['return_type']} {m['method']}({params})\n")
            f.write("\n")
    
    print(f"报告已生成: {output_path}")
    print(f"  总方法数: {total_methods}")
    print(f"  精确映射: {mapped_methods} ({mapped_methods/max(total_methods,1)*100:.1f}%)")
    print(f"  启发推断: {heuristic_methods} ({heuristic_methods/max(total_methods,1)*100:.1f}%)")
    print(f"  未映射: {len(unmapped_methods)} ({len(unmapped_methods)/max(total_methods,1)*100:.1f}%)")


# ==================== 集成接口 ====================

def create_smali_mapper(
    class_map: Dict[str, str],
    member_map: Dict[str, List[dict]],
    **kwargs
) -> SmaliEnhancedMapper:
    """创建 Smali 增强映射器"""
    return SmaliEnhancedMapper(class_map, member_map, **kwargs)


def create_smali_enhancer(mapper: SmaliEnhancedMapper, class_map: Dict[str, str]) -> SmaliCodeEnhancer:
    """创建 Smali 代码增强器"""
    return SmaliCodeEnhancer(mapper, class_map)


# ==================== 命令行接口 ====================

def main():
    parser = argparse.ArgumentParser(description='Smali 增强反混淆工具')
    parser.add_argument('--smali-dir', default=CONFIG['SMALI_DIR'], help='Smali 输出目录')
    parser.add_argument('--mapping-file', default=CONFIG['MAPPING_FILE'], help='映射文件路径')
    parser.add_argument('--output-dir', default=CONFIG['OUTPUT_DIR'], help='输出目录')
    parser.add_argument('--enable-heuristics', action='store_true', default=CONFIG['ENABLE_HEURISTICS'],
                        help='启用启发式命名')
    parser.add_argument('--heuristic-prefix', default=CONFIG['HEURISTIC_PREFIX'], help='启发式命名前缀')
    parser.add_argument('--workers', type=int, default=CONFIG['MAX_WORKERS'], help='并行工作线程数')
    
    args = parser.parse_args()
    
    # 更新配置
    CONFIG['SMALI_DIR'] = args.smali_dir
    CONFIG['MAPPING_FILE'] = args.mapping_file
    CONFIG['OUTPUT_DIR'] = args.output_dir
    CONFIG['ENABLE_HEURISTICS'] = args.enable_heuristics
    CONFIG['HEURISTIC_PREFIX'] = args.heuristic_prefix
    CONFIG['MAX_WORKERS'] = args.workers
    
    import sys
    # 动态添加模块路径
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    
    from process_java import parse_mapping
    
    # 加载映射
    print("加载映射文件...")
    class_map, member_map = parse_mapping(args.mapping_file)
    print(f"  类映射数: {len(class_map)}")
    print(f"  成员映射类数: {len(member_map)}")
    
    # 生成报告
    output_path = os.path.join(args.output_dir, 'unmapped_methods_report.txt')
    print("\n分析未映射方法...")
    generate_unmapped_method_report(class_map, member_map, output_path, smali_dir=args.smali_dir)


if __name__ == '__main__':
    main()
