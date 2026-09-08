"""Stable family filter values shared by SQL and saved search state."""

FORMAT_FAMILIES = {
    "@writer": ("文字文档", (".doc", ".docx", ".dot", ".rtf", ".wps", ".wpt", ".odt")),
    "@sheet": ("电子表格", (".xls", ".xlsx", ".xlsb", ".xlt", ".et", ".ett", ".etx", ".ettx", ".ods", ".csv", ".tsv")),
    "@slides": ("演示文稿", (".ppt", ".pptx", ".pps", ".dps", ".dpt", ".odp")),
}


def filter_extensions(value):
    return FORMAT_FAMILIES[value][1] if value in FORMAT_FAMILIES else (value,)
