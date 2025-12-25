#!/usr/bin/env python3
"""
Android SDK 接口映射模块

自动将实现标准 Android 接口的类中的混淆方法名映射到正确的接口方法名。

原理:
1. 从 Smali 文件中识别类实现的接口
2. 根据方法签名匹配接口方法
3. 自动应用正确的方法名
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


# ==================== Android SDK 接口定义 ====================

# 格式: interface_class -> {method_descriptor: method_name}
ANDROID_INTERFACE_METHODS: Dict[str, Dict[str, str]] = {
    # ==================== View 相关 ====================
    'android.view.View$OnClickListener': {
        '(Landroid/view/View;)V': 'onClick',
    },
    'android.view.View$OnLongClickListener': {
        '(Landroid/view/View;)Z': 'onLongClick',
    },
    'android.view.View$OnTouchListener': {
        '(Landroid/view/View;Landroid/view/MotionEvent;)Z': 'onTouch',
    },
    'android.view.View$OnKeyListener': {
        '(Landroid/view/View;ILandroid/view/KeyEvent;)Z': 'onKey',
    },
    'android.view.View$OnFocusChangeListener': {
        '(Landroid/view/View;Z)V': 'onFocusChange',
    },
    'android.view.View$OnScrollChangeListener': {
        '(Landroid/view/View;IIII)V': 'onScrollChange',
    },
    'android.view.View$OnLayoutChangeListener': {
        '(Landroid/view/View;IIIIIIII)V': 'onLayoutChange',
    },
    
    # ==================== AdapterView 相关 ====================
    'android.widget.AdapterView$OnItemClickListener': {
        '(Landroid/widget/AdapterView;Landroid/view/View;IJ)V': 'onItemClick',
    },
    'android.widget.AdapterView$OnItemLongClickListener': {
        '(Landroid/widget/AdapterView;Landroid/view/View;IJ)Z': 'onItemLongClick',
    },
    'android.widget.AdapterView$OnItemSelectedListener': {
        '(Landroid/widget/AdapterView;Landroid/view/View;IJ)V': 'onItemSelected',
        '(Landroid/widget/AdapterView;)V': 'onNothingSelected',
    },
    
    # ==================== TextWatcher ====================
    'android.text.TextWatcher': {
        '(Ljava/lang/CharSequence;III)V': 'beforeTextChanged',
        '(Ljava/lang/CharSequence;III)V': 'onTextChanged',
        '(Landroid/text/Editable;)V': 'afterTextChanged',
    },
    
    # ==================== CompoundButton ====================
    'android.widget.CompoundButton$OnCheckedChangeListener': {
        '(Landroid/widget/CompoundButton;Z)V': 'onCheckedChanged',
    },
    
    # ==================== SeekBar ====================
    'android.widget.SeekBar$OnSeekBarChangeListener': {
        '(Landroid/widget/SeekBar;IZ)V': 'onProgressChanged',
        '(Landroid/widget/SeekBar;)V': 'onStartTrackingTouch',
        '(Landroid/widget/SeekBar;)V': 'onStopTrackingTouch',
    },
    
    # ==================== Dialog 相关 ====================
    'android.content.DialogInterface$OnClickListener': {
        '(Landroid/content/DialogInterface;I)V': 'onClick',
    },
    'android.content.DialogInterface$OnCancelListener': {
        '(Landroid/content/DialogInterface;)V': 'onCancel',
    },
    'android.content.DialogInterface$OnDismissListener': {
        '(Landroid/content/DialogInterface;)V': 'onDismiss',
    },
    'android.content.DialogInterface$OnShowListener': {
        '(Landroid/content/DialogInterface;)V': 'onShow',
    },
    
    # ==================== Java 标准接口 ====================
    'java.lang.Runnable': {
        '()V': 'run',
    },
    'java.lang.Comparable': {
        '(Ljava/lang/Object;)I': 'compareTo',
    },
    'java.util.Comparator': {
        '(Ljava/lang/Object;Ljava/lang/Object;)I': 'compare',
    },
    'java.lang.Iterable': {
        '()Ljava/util/Iterator;': 'iterator',
    },
    'java.util.Iterator': {
        '()Z': 'hasNext',
        '()Ljava/lang/Object;': 'next',
        '()V': 'remove',
    },
    'java.io.Serializable': {},  # 标记接口，无方法
    'java.lang.Cloneable': {},   # 标记接口，无方法
    
    # ==================== Callback 接口 ====================
    'java.util.concurrent.Callable': {
        '()Ljava/lang/Object;': 'call',
    },
    'android.os.Handler$Callback': {
        '(Landroid/os/Message;)Z': 'handleMessage',
    },
    
    # ==================== Animation 相关 ====================
    'android.view.animation.Animation$AnimationListener': {
        '(Landroid/view/animation/Animation;)V': 'onAnimationStart',
        '(Landroid/view/animation/Animation;)V': 'onAnimationEnd',
        '(Landroid/view/animation/Animation;)V': 'onAnimationRepeat',
    },
    'android.animation.Animator$AnimatorListener': {
        '(Landroid/animation/Animator;)V': 'onAnimationStart',
        '(Landroid/animation/Animator;)V': 'onAnimationEnd',
        '(Landroid/animation/Animator;)V': 'onAnimationCancel',
        '(Landroid/animation/Animator;)V': 'onAnimationRepeat',
    },
    'android.animation.ValueAnimator$AnimatorUpdateListener': {
        '(Landroid/animation/ValueAnimator;)V': 'onAnimationUpdate',
    },
    
    # ==================== SurfaceHolder 相关 ====================
    'android.view.SurfaceHolder$Callback': {
        '(Landroid/view/SurfaceHolder;)V': 'surfaceCreated',
        '(Landroid/view/SurfaceHolder;III)V': 'surfaceChanged',
        '(Landroid/view/SurfaceHolder;)V': 'surfaceDestroyed',
    },
    
    # ==================== BroadcastReceiver ====================
    'android.content.BroadcastReceiver': {
        '(Landroid/content/Context;Landroid/content/Intent;)V': 'onReceive',
    },
    
    # ==================== Activity Lifecycle ====================
    'android.app.Application$ActivityLifecycleCallbacks': {
        '(Landroid/app/Activity;Landroid/os/Bundle;)V': 'onActivityCreated',
        '(Landroid/app/Activity;)V': 'onActivityStarted',
        '(Landroid/app/Activity;)V': 'onActivityResumed',
        '(Landroid/app/Activity;)V': 'onActivityPaused',
        '(Landroid/app/Activity;)V': 'onActivityStopped',
        '(Landroid/app/Activity;Landroid/os/Bundle;)V': 'onActivitySaveInstanceState',
        '(Landroid/app/Activity;)V': 'onActivityDestroyed',
    },
    
    # ==================== LocationListener ====================
    'android.location.LocationListener': {
        '(Landroid/location/Location;)V': 'onLocationChanged',
        '(Ljava/lang/String;)V': 'onProviderDisabled',
        '(Ljava/lang/String;)V': 'onProviderEnabled',
        '(Ljava/lang/String;ILandroid/os/Bundle;)V': 'onStatusChanged',
    },
    
    # ==================== SensorEventListener ====================
    'android.hardware.SensorEventListener': {
        '(Landroid/hardware/SensorEvent;)V': 'onSensorChanged',
        '(Landroid/hardware/Sensor;I)V': 'onAccuracyChanged',
    },
    
    # ==================== MediaPlayer 相关 ====================
    'android.media.MediaPlayer$OnPreparedListener': {
        '(Landroid/media/MediaPlayer;)V': 'onPrepared',
    },
    'android.media.MediaPlayer$OnCompletionListener': {
        '(Landroid/media/MediaPlayer;)V': 'onCompletion',
    },
    'android.media.MediaPlayer$OnErrorListener': {
        '(Landroid/media/MediaPlayer;II)Z': 'onError',
    },
    'android.media.MediaPlayer$OnBufferingUpdateListener': {
        '(Landroid/media/MediaPlayer;I)V': 'onBufferingUpdate',
    },
    'android.media.MediaPlayer$OnSeekCompleteListener': {
        '(Landroid/media/MediaPlayer;)V': 'onSeekComplete',
    },
    
    # ==================== WebView 相关 ====================
    'android.webkit.WebViewClient': {
        '(Landroid/webkit/WebView;Ljava/lang/String;)V': 'onPageStarted',
        '(Landroid/webkit/WebView;Ljava/lang/String;)V': 'onPageFinished',
        '(Landroid/webkit/WebView;ILjava/lang/String;Ljava/lang/String;)V': 'onReceivedError',
    },
    'android.webkit.WebChromeClient': {
        '(Landroid/webkit/WebView;I)V': 'onProgressChanged',
        '(Landroid/webkit/WebView;Ljava/lang/String;)V': 'onReceivedTitle',
    },
    
    # ==================== GestureDetector ====================
    'android.view.GestureDetector$OnGestureListener': {
        '(Landroid/view/MotionEvent;)Z': 'onDown',
        '(Landroid/view/MotionEvent;)V': 'onShowPress',
        '(Landroid/view/MotionEvent;)Z': 'onSingleTapUp',
        '(Landroid/view/MotionEvent;Landroid/view/MotionEvent;FF)Z': 'onScroll',
        '(Landroid/view/MotionEvent;)V': 'onLongPress',
        '(Landroid/view/MotionEvent;Landroid/view/MotionEvent;FF)Z': 'onFling',
    },
    'android.view.GestureDetector$OnDoubleTapListener': {
        '(Landroid/view/MotionEvent;)Z': 'onSingleTapConfirmed',
        '(Landroid/view/MotionEvent;)Z': 'onDoubleTap',
        '(Landroid/view/MotionEvent;)Z': 'onDoubleTapEvent',
    },
    
    # ==================== ScaleGestureDetector ====================
    'android.view.ScaleGestureDetector$OnScaleGestureListener': {
        '(Landroid/view/ScaleGestureDetector;)Z': 'onScale',
        '(Landroid/view/ScaleGestureDetector;)Z': 'onScaleBegin',
        '(Landroid/view/ScaleGestureDetector;)V': 'onScaleEnd',
    },
    
    # ==================== RecyclerView (AndroidX) ====================
    'androidx.recyclerview.widget.RecyclerView$Adapter': {
        '(Landroid/view/ViewGroup;I)Landroidx/recyclerview/widget/RecyclerView$ViewHolder;': 'onCreateViewHolder',
        '(Landroidx/recyclerview/widget/RecyclerView$ViewHolder;I)V': 'onBindViewHolder',
        '()I': 'getItemCount',
    },
}


# ==================== 接口映射器 ====================

class AndroidInterfaceMapper:
    """
    Android SDK 接口映射器
    
    根据类实现的接口和方法签名，自动推断混淆方法的原始名称
    """
    
    def __init__(self):
        self.interface_methods = ANDROID_INTERFACE_METHODS
        
        # 构建签名到方法名的快速索引
        # {signature: [(interface, method_name), ...]}
        self._sig_index: Dict[str, List[Tuple[str, str]]] = {}
        self._build_signature_index()
    
    def _build_signature_index(self):
        """构建签名索引"""
        for interface, methods in self.interface_methods.items():
            for sig, name in methods.items():
                if sig not in self._sig_index:
                    self._sig_index[sig] = []
                self._sig_index[sig].append((interface, name))
    
    def get_method_name_by_interface(
        self,
        interfaces: List[str],
        method_descriptor: str
    ) -> Optional[Tuple[str, str]]:
        """
        根据接口和方法签名获取方法名
        
        Args:
            interfaces: 类实现的接口列表
            method_descriptor: 方法描述符，如 (Landroid/view/View;)V
        
        Returns:
            (interface_name, method_name) 或 None
        """
        # 标准化接口名
        normalized_interfaces = set()
        for iface in interfaces:
            # 处理 Smali 格式: Landroid/view/View$OnClickListener; -> android.view.View$OnClickListener
            if iface.startswith('L') and iface.endswith(';'):
                iface = iface[1:-1].replace('/', '.')
            else:
                iface = iface.replace('/', '.')
            normalized_interfaces.add(iface)
        
        # 在签名索引中查找
        if method_descriptor in self._sig_index:
            for interface, method_name in self._sig_index[method_descriptor]:
                if interface in normalized_interfaces:
                    return (interface, method_name)
        
        return None
    
    def infer_methods_for_class(
        self,
        interfaces: List[str],
        methods: List[Tuple[str, str]]  # [(method_name, descriptor), ...]
    ) -> Dict[str, str]:
        """
        为类中的方法推断接口方法名
        
        Args:
            interfaces: 类实现的接口列表
            methods: 类中的方法列表 [(name, descriptor), ...]
        
        Returns:
            {obfuscated_name: interface_method_name}
        """
        mappings = {}
        
        for method_name, descriptor in methods:
            result = self.get_method_name_by_interface(interfaces, descriptor)
            if result:
                interface, real_name = result
                if method_name != real_name:  # 只记录不同的
                    mappings[method_name] = real_name
        
        return mappings
    
    def get_all_interface_methods(self) -> Dict[str, Dict[str, str]]:
        """获取所有接口方法定义"""
        return self.interface_methods
    
    def get_interface_count(self) -> int:
        """获取接口数量"""
        return len(self.interface_methods)
    
    def get_method_count(self) -> int:
        """获取方法数量"""
        return sum(len(methods) for methods in self.interface_methods.values())


# ==================== 集成函数 ====================

def create_android_mapper() -> AndroidInterfaceMapper:
    """创建 Android 接口映射器"""
    return AndroidInterfaceMapper()


# ==================== 测试 ====================

if __name__ == '__main__':
    mapper = create_android_mapper()
    
    print("=== Android SDK 接口映射器 ===")
    print(f"已定义接口数: {mapper.get_interface_count()}")
    print(f"已定义方法数: {mapper.get_method_count()}")
    
    # 测试
    print("\n=== 测试 ===")
    
    # 模拟一个实现 OnClickListener 的类
    test_interfaces = ['Landroid/view/View$OnClickListener;']
    test_methods = [
        ('a', '(Landroid/view/View;)V'),  # 混淆的 onClick
        ('b', '()V'),                      # 其他方法
    ]
    
    mappings = mapper.infer_methods_for_class(test_interfaces, test_methods)
    print(f"输入接口: {test_interfaces}")
    print(f"输入方法: {test_methods}")
    print(f"推断结果: {mappings}")
