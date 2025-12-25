#!/usr/bin/env python3
"""
交叉引用分析模块 (XRef & Call Graph)

功能:
1. 构建全局字段引用索引 (Field XRef)
2. 构建方法调用图 (Call Graph)
3. 从调用关系推断方法名
"""

import os
import re
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==================== 数据结构 ====================

@dataclass
class MethodReference:
    """方法引用"""
    caller_class: str
    caller_method: str
    callee_class: str
    callee_method: str
    callee_descriptor: str
    invoke_type: str  # invoke-virtual, invoke-static, etc.
    line_number: int = 0


@dataclass
class FieldReference:
    """字段引用"""
    accessor_class: str
    accessor_method: str
    field_class: str
    field_name: str
    field_type: str
    access_type: str  # iget, iput, sget, sput
    line_number: int = 0


@dataclass
class XRefIndex:
    """交叉引用索引"""
    # 方法调用图
    # {(class, method, descriptor): [MethodReference, ...]}
    method_callers: Dict[Tuple[str, str, str], List[MethodReference]] = field(default_factory=lambda: defaultdict(list))
    method_callees: Dict[Tuple[str, str, str], List[MethodReference]] = field(default_factory=lambda: defaultdict(list))
    
    # 字段引用
    # {(class, field): [FieldReference, ...]}
    field_readers: Dict[Tuple[str, str], List[FieldReference]] = field(default_factory=lambda: defaultdict(list))
    field_writers: Dict[Tuple[str, str], List[FieldReference]] = field(default_factory=lambda: defaultdict(list))


# ==================== Smali 指令解析 ====================

# invoke 指令正则 - 标准 Smali 格式: invoke-xxx {regs}, Lclass;->method(params)ret
INVOKE_PATTERN_STANDARD = re.compile(
    r'(invoke-\w+)\s*(?:/range)?\s*\{[^}]*\},\s*'
    r'(L[^;]+;)->(\w+)\(([^)]*)\)([^\s]+)'
)

# invoke 指令正则 - 简化格式: invoke-xxx class.method:(params)ret 或 class/method:(params)ret
INVOKE_PATTERN_SIMPLE = re.compile(
    r'(invoke-\w+)\s+([a-zA-Z0-9_$/]+)\.?"?([<>\w]+)"?:\(([^)]*)\)([^\s]+)'
)

# 字段访问指令正则 - 标准格式: iget vX, Lclass;->field:type
FIELD_ACCESS_PATTERN_STANDARD = re.compile(
    r'([si](?:get|put)(?:-\w+)?)\s+[vp]\d+,\s*'
    r'(L[^;]+;)->(\w+):([^\s]+)'
)

# 字段访问指令正则 - 简化格式: iget field:type 或 iget class.field:type
FIELD_ACCESS_PATTERN_SIMPLE = re.compile(
    r'^\s*(iget|iput|sget|sput)\s+([a-zA-Z0-9_$/]+)?\.?(\w+):([^\s]+)'
)


class SmaliXRefParser:
    """Smali 交叉引用解析器"""
    
    def __init__(self, smali_dir: str):
        self.smali_dir = smali_dir
        self.xref = XRefIndex()
        
        # 统计
        self.stats = {
            'files_parsed': 0,
            'method_refs': 0,
            'field_refs': 0,
        }
    
    def parse_smali_file(self, smali_path: str) -> None:
        """解析单个 Smali 文件"""
        try:
            with open(smali_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            return
        
        self.stats['files_parsed'] += 1
        
        # 解析类名
        class_match = re.search(r'\.class\s+.*?(L[^;]+;)', content)
        if not class_match:
            return
        
        current_class = class_match.group(1)
        current_method = None
        current_descriptor = None
        
        lines = content.split('\n')
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            
            # 检测方法定义
            method_match = re.match(r'\.method\s+.*?(\S+)\(([^)]*)\)(\S+)', line)
            if method_match:
                current_method = method_match.group(1)
                params = method_match.group(2)
                ret = method_match.group(3)
                current_descriptor = f"({params}){ret}"
                continue
            
            if line == '.end method':
                current_method = None
                current_descriptor = None
                continue
            
            if not current_method:
                continue
            
            # 解析 invoke 指令 (尝试两种格式)
            invoke_match = INVOKE_PATTERN_STANDARD.search(line)
            if not invoke_match:
                invoke_match = INVOKE_PATTERN_SIMPLE.search(line)
            
            if invoke_match:
                invoke_type = invoke_match.group(1)
                callee_class = invoke_match.group(2)
                callee_method = invoke_match.group(3)
                callee_params = invoke_match.group(4)
                callee_ret = invoke_match.group(5)
                callee_descriptor = f"({callee_params}){callee_ret}"
                
                # 标准化类名格式
                if not callee_class.startswith('L'):
                    callee_class = 'L' + callee_class.replace('.', '/') + ';'
                
                ref = MethodReference(
                    caller_class=current_class,
                    caller_method=current_method,
                    callee_class=callee_class,
                    callee_method=callee_method,
                    callee_descriptor=callee_descriptor,
                    invoke_type=invoke_type,
                    line_number=line_num
                )
                
                # 记录调用关系
                caller_key = (current_class, current_method, current_descriptor)
                callee_key = (callee_class, callee_method, callee_descriptor)
                
                self.xref.method_callees[caller_key].append(ref)
                self.xref.method_callers[callee_key].append(ref)
                self.stats['method_refs'] += 1
                continue
            
            # 解析字段访问指令 (尝试两种格式)
            field_match = FIELD_ACCESS_PATTERN_STANDARD.search(line)
            if not field_match:
                field_match = FIELD_ACCESS_PATTERN_SIMPLE.search(line)
            
            if field_match:
                access_type = field_match.group(1)
                field_class = field_match.group(2) or current_class
                field_name = field_match.group(3)
                field_type = field_match.group(4)
                
                # 标准化类名格式
                if not field_class.startswith('L'):
                    field_class = 'L' + field_class.replace('.', '/') + ';'
                
                ref = FieldReference(
                    accessor_class=current_class,
                    accessor_method=current_method,
                    field_class=field_class,
                    field_name=field_name,
                    field_type=field_type,
                    access_type=access_type,
                    line_number=line_num
                )
                
                field_key = (field_class, field_name)
                
                if 'get' in access_type:
                    self.xref.field_readers[field_key].append(ref)
                else:
                    self.xref.field_writers[field_key].append(ref)
                
                self.stats['field_refs'] += 1
    
    def parse_all_files(self, max_workers: int = 4) -> XRefIndex:
        """并行解析所有 Smali 文件"""
        smali_files = []
        
        for root, dirs, files in os.walk(self.smali_dir):
            for file in files:
                if file.endswith('.smali'):
                    smali_files.append(os.path.join(root, file))
        
        print(f"扫描 Smali 文件: {len(smali_files)} 个")
        
        # 并行解析
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.parse_smali_file, path): path for path in smali_files}
            
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"解析失败: {futures[future]}: {e}")
        
        print(f"解析完成:")
        print(f"  文件数: {self.stats['files_parsed']}")
        print(f"  方法引用数: {self.stats['method_refs']}")
        print(f"  字段引用数: {self.stats['field_refs']}")
        
        return self.xref


# ==================== 调用图分析器 ====================

class CallGraphAnalyzer:
    """
    调用图分析器
    
    功能:
    1. 从已知语义的调用者推断被调用方法
    2. 从方法的调用者上下文推断语义
    """
    
    # 已知语义的方法模式
    SEMANTIC_PATTERNS = {
        # 绘制相关
        'draw': ['onDraw', 'draw', 'paint', 'render'],
        'update': ['update', 'tick', 'onUpdate', 'step'],
        'init': ['init', 'initialize', 'setup', 'onCreate'],
        'dispose': ['dispose', 'cleanup', 'destroy', 'close', 'release'],
        'callback': ['onClick', 'onTouch', 'onEvent', 'handle'],
    }
    
    def __init__(self, xref: XRefIndex, class_map: Dict[str, str] = None):
        self.xref = xref
        self.class_map = class_map or {}
        
        # 已知语义的方法
        self._known_semantic_methods: Dict[str, str] = {}
    
    def infer_from_callers(
        self,
        target_class: str,
        target_method: str,
        target_descriptor: str
    ) -> Optional[str]:
        """
        从调用者推断方法语义
        
        如果大多数调用者来自 draw/render 类方法，则目标方法可能也是绘制相关
        """
        key = (target_class, target_method, target_descriptor)
        callers = self.xref.method_callers.get(key, [])
        
        if not callers:
            return None
        
        # 统计调用者的语义类别
        semantic_counts = defaultdict(int)
        
        for ref in callers:
            caller_name = ref.caller_method.lower()
            
            for category, patterns in self.SEMANTIC_PATTERNS.items():
                for pattern in patterns:
                    if pattern.lower() in caller_name:
                        semantic_counts[category] += 1
                        break
        
        if not semantic_counts:
            return None
        
        # 选择最常见的语义类别
        best_category = max(semantic_counts, key=semantic_counts.get)
        count = semantic_counts[best_category]
        
        # 至少需要 2 个调用者有相同语义
        if count >= 2:
            return f"relatedTo{best_category.capitalize()}"
        
        return None
    
    def get_method_callers(
        self,
        target_class: str,
        target_method: str,
        target_descriptor: str
    ) -> List[MethodReference]:
        """获取方法的所有调用者"""
        key = (target_class, target_method, target_descriptor)
        return self.xref.method_callers.get(key, [])
    
    def get_method_callees(
        self,
        caller_class: str,
        caller_method: str,
        caller_descriptor: str
    ) -> List[MethodReference]:
        """获取方法调用的所有方法"""
        key = (caller_class, caller_method, caller_descriptor)
        return self.xref.method_callees.get(key, [])
    
    def get_field_accessors(
        self,
        field_class: str,
        field_name: str
    ) -> Tuple[List[FieldReference], List[FieldReference]]:
        """获取字段的读写访问者"""
        key = (field_class, field_name)
        readers = self.xref.field_readers.get(key, [])
        writers = self.xref.field_writers.get(key, [])
        return readers, writers
    
    def get_call_graph_stats(self) -> Dict[str, int]:
        """获取调用图统计"""
        return {
            'unique_methods_called': len(self.xref.method_callers),
            'unique_methods_calling': len(self.xref.method_callees),
            'unique_fields_accessed': len(self.xref.field_readers) + len(self.xref.field_writers),
            'total_method_refs': sum(len(refs) for refs in self.xref.method_callers.values()),
            'total_field_refs': sum(len(refs) for refs in self.xref.field_readers.values()) + 
                               sum(len(refs) for refs in self.xref.field_writers.values()),
        }


# ==================== 主函数 ====================

def build_xref_index(smali_dir: str, max_workers: int = 4) -> Tuple[XRefIndex, CallGraphAnalyzer]:
    """
    构建交叉引用索引和调用图分析器
    
    Returns:
        (XRefIndex, CallGraphAnalyzer)
    """
    parser = SmaliXRefParser(smali_dir)
    xref = parser.parse_all_files(max_workers=max_workers)
    analyzer = CallGraphAnalyzer(xref)
    
    return xref, analyzer


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='交叉引用分析')
    parser.add_argument('--smali-dir', default='/Users/hoto/PC_Java/smali_output', help='Smali 目录')
    parser.add_argument('--workers', type=int, default=4, help='并行工作线程数')
    
    args = parser.parse_args()
    
    print("=== 交叉引用分析 ===\n")
    
    xref, analyzer = build_xref_index(args.smali_dir, args.workers)
    
    stats = analyzer.get_call_graph_stats()
    print(f"\n=== 调用图统计 ===")
    print(f"  唯一被调用方法数: {stats['unique_methods_called']}")
    print(f"  唯一调用者方法数: {stats['unique_methods_calling']}")
    print(f"  唯一被访问字段数: {stats['unique_fields_accessed']}")
    print(f"  总方法引用数: {stats['total_method_refs']}")
    print(f"  总字段引用数: {stats['total_field_refs']}")


if __name__ == '__main__':
    main()
