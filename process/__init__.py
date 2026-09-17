"""中药饮片炮制领域契约与质量放行平台。

入口：
* :class:`process.batch.ProcessingPlatform` —— 平台主对象；
* :func:`process.scenario.load_fixture` —— 加载样例批记录；
* ``python -m process.demo`` —— 质量受权人核对视图；
* ``python -m process.demo --check`` —— 样例台账机器核对。
"""

from .batch import ProcessingPlatform
from .specs import SpecificationRegistry, jiuzhi_danggui_spec

__all__ = ["ProcessingPlatform", "SpecificationRegistry", "jiuzhi_danggui_spec"]
