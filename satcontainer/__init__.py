"""
SatContainer - 卫星容器启动优化框架

针对卫星计算载荷的容器冷启动优化，通过检查点自动发现、
镜像布局优化和快速缓存预热来最小化上电到应用就绪的时间。
"""

__version__ = "0.1.0"
__author__ = "SatContainer Team"

from satcontainer.config import Config
