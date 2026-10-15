"""Installed font labels; warnings are explicit instead of silent substitution."""
from functools import lru_cache

@lru_cache(maxsize=1)
def installed_fonts():
    names={'Arial','Segoe UI','Tahoma','Times New Roman'}
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        name=winreg.EnumValue(key,i)[0].split(' (')[0]
                        names.add(name)
                        names.update(part.strip() for part in name.split(' & '))
            except OSError:pass
    except ImportError:pass
    return sorted(names)

def font_warnings(project):
    known={name.casefold() for name in installed_fonts()}
    chosen={project['params'].get('font','Arial')}
    chosen.update(r.get('subtitle_style',{}).get('font','Arial') for r in project['segments'])
    return [f'Phông “{name}” chưa được xác nhận có trên máy; chọn phông đã cài trước khi xuất để tránh đổi kiểu.'
            for name in sorted(chosen) if name.casefold() not in known]
