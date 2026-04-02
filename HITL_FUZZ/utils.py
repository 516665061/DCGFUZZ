# utils.py
import os
import json
import threading
from datetime import datetime
from typing import Any, Dict

def timestamp() -> str:
    """生成符合文件命名规范的 ISO 格式时间戳"""
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p: str):
    """确保目录存在，如果不存在则创建"""
    if p and not os.path.exists(p):
        os.makedirs(p, exist_ok=True)

class JsonLogger:
    """
    线程安全的追加式 JSON 日志记录器。
    采用 'Read-Modify-Write' 模式并配合线程锁，确保数据一致性。
    """
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock = threading.Lock()
        
        # 初始化时确保目录存在
        ensure_dir(os.path.dirname(self.filepath))
        
        # 如果文件不存在，初始化为一个空列表
        if not os.path.exists(self.filepath):
            self._write_file([])

    def _read_file(self) -> list:
        """读取当前的 JSON 列表数据"""
        try:
            if os.path.exists(self.filepath) and os.path.getsize(self.filepath) > 0:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
        return []

    def _write_file(self, data: list):
        """原子化写入 JSON 数据"""
        tmp_path = self.filepath + ".tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno()) # 确保刷入磁盘
            # 原子替换
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            print(f"[Utils] Error writing log file: {e}")

    def append(self, record: Dict[str, Any]):
        """
        向 JSON 文件中追加一条记录（线程安全）
        :param record: 要写入的字典数据
        """
        with self.lock:
            data = self._read_file()
            # 自动补充写入时间
            if "timestamp" not in record:
                record["log_time"] = timestamp()
            data.append(record)
            self._write_file(data)