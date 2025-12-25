"""
高级反混淆增强模块

功能:
- 启发式局部变量命名
- 高频模式识别 
- 未映射成员收集
- 映射表自动扩展
"""

import re
from typing import Dict, List, Set, Tuple, Optional
from collections import Counter, defaultdict


# ==================== 类型到名称的映射 ====================

TYPE_NAME_HINTS = {
    # 基本类型
    'int': 'n', 'long': 'l', 'float': 'f', 'double': 'd',
    'boolean': 'flag', 'byte': 'b', 'char': 'c', 'short': 's',
    'String': 'str', 'Object': 'obj',
    
    # 集合类型
    'List': 'list', 'ArrayList': 'list', 'LinkedList': 'list',
    'Map': 'map', 'HashMap': 'map', 'TreeMap': 'map',
    'Set': 'set', 'HashSet': 'set', 'TreeSet': 'set',
    'Collection': 'collection', 'Iterator': 'iter',
    
    # 数组
    'int[]': 'arr', 'byte[]': 'data', 'String[]': 'strs',
    
    # IO
    'InputStream': 'input', 'OutputStream': 'output',
    'Reader': 'reader', 'Writer': 'writer',
    'File': 'file', 'Path': 'path',
    
    # 游戏相关 (从映射推断)
    'GameEngine': 'engine', 'PlayerTeam': 'team',
    'GameInputStream': 'gameInput', 'GameOutputStream': 'gameOutput',
}

# 循环变量名序列
LOOP_VAR_NAMES = ['i', 'j', 'k', 'm', 'n']

# 上下文模式
CONTEXT_PATTERNS = {
    'exception': ['Exception', 'Error', 'Throwable'],
    'event': ['Event', 'Listener', 'Callback'],
    'view': ['View', 'Widget', 'Component'],
    'position': ['Point', 'PointF', 'Rect', 'RectF'],
}


# ==================== 动态类型命名表构建 ====================

def build_type_hints_from_class_map(class_map: Dict[str, str]) -> Dict[str, str]:
    """
    从类映射表动态构建类型命名表
    
    Args:
        class_map: {obf_class: orig_class}
        
    Returns:
        扩展的类型命名表 {TypeName: varName}
    """
    hints = dict(TYPE_NAME_HINTS)  # 保留基础表
    
    for orig_full in class_map.values():
        short_name = orig_full.split('.')[-1]
        # 跳过内部类和已存在的映射
        if not short_name or '$' in short_name or short_name in hints:
            continue
        # 转为小驼峰
        if len(short_name) > 1:
            var_name = short_name[0].lower() + short_name[1:]
        else:
            var_name = short_name.lower()
        hints[short_name] = var_name
    
    return hints


# ==================== 启发式命名器 ====================

class HeuristicNamer:
    """
    启发式变量命名器
    """
    
    def __init__(self, type_hints: Dict[str, str] = None):
        self.type_hints = type_hints if type_hints else TYPE_NAME_HINTS
        self.type_counters: Dict[str, int] = defaultdict(int)
        self.loop_depth = 0
        self.used_names: Set[str] = set()
    
    def reset(self):
        """重置状态"""
        self.type_counters.clear()
        self.loop_depth = 0
        self.used_names.clear()
    
    def infer_name(self, var_type: str, context: str = '') -> str:
        """
        根据类型推断变量名
        
        Args:
            var_type: 变量类型
            context: 上下文信息（如 'loop', 'catch'）
        """
        # 处理泛型
        base_type = re.sub(r'<.*>', '', var_type).strip()
        base_type = re.sub(r'\[\]', '', base_type).strip()
        
        # 特殊上下文
        if context == 'loop':
            if self.loop_depth < len(LOOP_VAR_NAMES):
                name = LOOP_VAR_NAMES[self.loop_depth]
                self.loop_depth += 1
                return name
        
        if context == 'catch':
            return 'ex'
        
        # 从类型推断
        if base_type in self.type_hints:
            base_name = self.type_hints[base_type]
        else:
            # 使用类型名的小写形式
            base_name = self._to_camel_case(base_type)
        
        # 处理重复名称
        count = self.type_counters[base_type]
        self.type_counters[base_type] += 1
        
        if count == 0:
            name = base_name
        else:
            name = f"{base_name}{count + 1}"
        
        self.used_names.add(name)
        return name
    
    def _to_camel_case(self, name: str) -> str:
        """转换为小驼峰"""
        if not name:
            return 'var'
        # 处理全大写
        if name.isupper():
            return name.lower()
        # 首字母小写
        return name[0].lower() + name[1:] if len(name) > 1 else name.lower()


# ==================== 模式识别器 ====================

class PatternRecognizer:
    """
    代码模式识别器
    """
    
    # 模式正则
    PATTERNS = {
        # for 循环
        'for_loop': re.compile(
            r'for\s*\(\s*(?:int|long)\s+(\w+)\s*=\s*\d+\s*;',
            re.MULTILINE
        ),
        
        # foreach 循环
        'foreach': re.compile(
            r'for\s*\(\s*(\w+(?:<[^>]+>)?)\s+(\w+)\s*:\s*(\w+)',
            re.MULTILINE
        ),
        
        # null 检查
        'null_check': re.compile(
            r'if\s*\(\s*(\w+)\s*[!=]=\s*null\s*\)',
            re.MULTILINE
        ),
        
        # getter 模式
        'getter': re.compile(
            r'(?:public|protected|private)?\s*(\w+)\s+get(\w+)\s*\(\s*\)',
            re.MULTILINE
        ),
        
        # setter 模式
        'setter': re.compile(
            r'(?:public|protected|private)?\s*void\s+set(\w+)\s*\(\s*(\w+)\s+(\w+)\s*\)',
            re.MULTILINE
        ),
        
        # 常量模式
        'constant': re.compile(
            r'(?:public|private|protected)?\s*static\s+final\s+(\w+)\s+(\w+)\s*=',
            re.MULTILINE
        ),
        
        # 事件处理器
        'event_handler': re.compile(
            r'(?:public|protected|private)?\s*void\s+on(\w+)\s*\(',
            re.MULTILINE
        ),
        
        # try-catch
        'try_catch': re.compile(
            r'catch\s*\(\s*(\w+(?:\s*\|\s*\w+)*)\s+(\w+)\s*\)',
            re.MULTILINE
        ),
        
        # 局部变量声明: Type varName = ... 或 Type varName;
        'local_var_decl': re.compile(
            r'\b([A-Z][a-zA-Z0-9_]*)\s+([a-z][a-zA-Z0-9]?)\s*[=;]',
            re.MULTILINE
        ),
    }
    
    def analyze(self, code: str) -> Dict[str, List[dict]]:
        """分析代码中的模式"""
        results = {}
        
        for name, pattern in self.PATTERNS.items():
            matches = []
            for match in pattern.finditer(code):
                matches.append({
                    'groups': match.groups(),
                    'start': match.start(),
                    'end': match.end(),
                    'text': match.group(0)
                })
            if matches:
                results[name] = matches
        
        return results
    
    def infer_field_from_getter_setter(self, code: str) -> Dict[str, str]:
        """从 getter/setter 推断字段名"""
        fields = {}
        
        # 从 getter 推断
        for match in self.PATTERNS['getter'].finditer(code):
            ret_type, name = match.groups()
            field_name = name[0].lower() + name[1:] if len(name) > 1 else name.lower()
            fields[field_name] = ret_type
        
        # 从 setter 推断
        for match in self.PATTERNS['setter'].finditer(code):
            name, param_type, param_name = match.groups()
            field_name = name[0].lower() + name[1:] if len(name) > 1 else name.lower()
            if field_name not in fields:
                fields[field_name] = param_type
        
        return fields


# ==================== 未映射成员收集器 ====================

class UnmappedCollector:
    """
    未映射成员收集器
    """
    
    def __init__(self):
        # (class, type, member) -> count
        self.unmapped: Counter = Counter()
    
    def add(self, cls: str, member_type: str, member: str):
        """添加未映射成员"""
        self.unmapped[(cls, member_type, member)] += 1
    
    def get_top(self, n: int = 100) -> List[Tuple[Tuple[str, str, str], int]]:
        """获取出现次数最多的未映射成员"""
        return self.unmapped.most_common(n)
    
    def export(self, filepath: str):
        """导出到文件"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# 未映射成员统计\n")
            f.write("# 格式: class, type, member, count\n\n")
            for (cls, mtype, member), count in self.unmapped.most_common():
                f.write(f"{cls}, {mtype}, {member}, {count}\n")
    
    def analyze(self) -> Dict[str, any]:
        """分析统计"""
        total = sum(self.unmapped.values())
        unique = len(self.unmapped)
        
        by_type = defaultdict(int)
        for (cls, mtype, member), count in self.unmapped.items():
            by_type[mtype] += count
        
        return {
            'total_occurrences': total,
            'unique_members': unique,
            'by_type': dict(by_type),
        }


# ==================== 映射扩展器 ====================

class MappingExtender:
    """
    映射表扩展器
    """
    
    def __init__(self, class_map: Dict[str, str], member_map: Dict[str, List[dict]]):
        self.class_map = class_map
        self.member_map = member_map
        self.extended_mappings: Dict[str, Dict[str, str]] = defaultdict(dict)
    
    def extend_from_patterns(self, code: str, obf_class: str):
        """从代码模式扩展映射"""
        recognizer = PatternRecognizer()
        patterns = recognizer.analyze(code)
        
        # 从 getter/setter 推断字段
        fields = recognizer.infer_field_from_getter_setter(code)
        for field_name, field_type in fields.items():
            if len(field_name) <= 2:  # 可能是混淆名
                continue
            # 查找对应的混淆字段
            if obf_class in self.member_map:
                for m in self.member_map[obf_class]:
                    if not m['is_method'] and m['orig'] == field_name:
                        # 已有映射
                        break
                else:
                    # 可能是新发现的字段
                    self.extended_mappings[obf_class][field_name] = field_name
    
    def get_extended(self) -> Dict[str, Dict[str, str]]:
        """获取扩展的映射"""
        return dict(self.extended_mappings)


# ==================== 代码增强器 ====================

class CodeEnhancer:
    """
    代码增强器 - 应用启发式命名
    """
    
    def __init__(self, class_map: Dict[str, str], member_map: Dict[str, List[dict]]):
        self.class_map = class_map
        self.member_map = member_map
        # 构建扩展的类型命名表
        self.type_hints = build_type_hints_from_class_map(class_map)
        self.namer = HeuristicNamer(self.type_hints)
        self.recognizer = PatternRecognizer()
        self.collector = UnmappedCollector()
    
    def enhance(self, code: str, obf_class: str) -> str:
        """增强代码"""
        self.namer.reset()
        
        # 分析模式
        patterns = self.recognizer.analyze(code)
        
        # 收集需要替换的变量
        replacements = []
        
        # 处理 for 循环变量
        for match in patterns.get('for_loop', []):
            var_name = match['groups'][0]
            if len(var_name) <= 2 and var_name.islower():
                new_name = self.namer.infer_name('int', 'loop')
                if new_name != var_name:
                    replacements.append((var_name, new_name, match['start'], match['end']))
        
        # 处理 catch 变量
        for match in patterns.get('try_catch', []):
            exc_type, var_name = match['groups']
            if len(var_name) <= 2 and var_name.islower():
                new_name = self.namer.infer_name(exc_type, 'catch')
                if new_name != var_name:
                    replacements.append((var_name, new_name, match['start'], match['end']))
        
        # 处理局部变量声明 (NEW)
        for match in patterns.get('local_var_decl', []):
            var_type, var_name = match['groups']
            # 仅处理短变量名（1-2字符）且类型在类型表中
            if len(var_name) <= 2 and var_name.islower() and var_type in self.type_hints:
                new_name = self.namer.infer_name(var_type)
                if new_name != var_name and new_name not in self.namer.used_names:
                    replacements.append((var_name, new_name, match['start'], match['end']))
        
        # 应用替换（简化版，使用全局替换）
        for old_name, new_name, start, end in replacements:
            # 只替换独立的标识符
            pattern = r'\b' + re.escape(old_name) + r'\b'
            code = re.sub(pattern, new_name, code)
        
        return code
    
    def get_unmapped_stats(self) -> Dict:
        """获取未映射统计"""
        return self.collector.analyze()


# ==================== 工厂函数 ====================

def create_enhancer(class_map: Dict[str, str], 
                    member_map: Dict[str, List[dict]]) -> CodeEnhancer:
    """创建代码增强器"""
    return CodeEnhancer(class_map, member_map)


def create_namer() -> HeuristicNamer:
    """创建命名器"""
    return HeuristicNamer()


def create_recognizer() -> PatternRecognizer:
    """创建模式识别器"""
    return PatternRecognizer()


def create_collector() -> UnmappedCollector:
    """创建收集器"""
    return UnmappedCollector()
