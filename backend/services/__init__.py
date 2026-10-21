"""校园事务与宿舍服务业务层。"""
from .affairs import AffairError, AffairService, AffairSpec, load_affair_specs
from .dormitory import DormitoryError, DormitoryService

__all__ = [
    "AffairError",
    "AffairService",
    "AffairSpec",
    "DormitoryError",
    "DormitoryService",
    "load_affair_specs",
]
