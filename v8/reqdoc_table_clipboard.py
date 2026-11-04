"""Portable table clipboard text preparation (TSV for Excel and text editors)."""


def _clean(value):
    return str(value).replace('\r\n',' ').replace('\n',' ').replace('\r',' ').replace('\t',' ')


def rows_to_tsv(headers,rows):
    """Copy a rectangular table to tab-separated text without exposing row IDs."""
    items=([list(headers)] if headers else [])+[list(row) for row in rows]
    if not items:return ''
    width=max(len(row) for row in items)
    return '\r\n'.join('\t'.join(_clean(row[i]) if i<len(row) else '' for i in range(width)) for row in items)
