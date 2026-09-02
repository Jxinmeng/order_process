"""Excel 写入器。"""

from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook


class ExcelWriter:
    """将字典列表写入 Excel。"""

    @staticmethod
    def write(data: List[Dict[str, Any]], file_path: str) -> None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()
        sheet = workbook.active
        if data:
            headers = list(dict.fromkeys(key for row in data for key in row))
            sheet.append(headers)
            for row in data:
                sheet.append([row.get(header) for header in headers])
        workbook.save(path)
