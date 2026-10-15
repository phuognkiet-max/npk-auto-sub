"""NPK identity for Windows taskbar grouping and user-initiated pin/relaunch.

Only touches windows owned by this process. No Explorer restart, pin-list edits,
registry changes, or modification of the shared Python executable.
"""
import ctypes as C
import logging
import os
from pathlib import Path
import uuid

APP_ID = 'NPK.AutoSub.Desktop'
APP_NAME = 'NPK Auto Sub'
ROOT = Path(__file__).resolve().parent
ICON = ROOT / 'static/npk-app-icon.ico'


def _checked(hr):
    if hr < 0:
        raise OSError(f'Windows identity HRESULT 0x{hr & 0xffffffff:08X}')


def set_process_identity():
    if os.name != 'nt':
        return
    shell = C.WinDLL('shell32')
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [C.c_wchar_p]
    shell.SetCurrentProcessExplicitAppUserModelID.restype = C.c_long
    _checked(shell.SetCurrentProcessExplicitAppUserModelID(APP_ID))


class GUID(C.Structure):
    _fields_ = [('Data1', C.c_uint32), ('Data2', C.c_uint16),
                ('Data3', C.c_uint16), ('Data4', C.c_ubyte * 8)]


class PropertyKey(C.Structure):
    _fields_ = [('fmtid', GUID), ('pid', C.c_uint32)]


class Blob(C.Structure):
    _fields_ = [('size', C.c_uint32), ('data', C.c_void_p)]


class VariantData(C.Union):
    _fields_ = [('pointer', C.c_void_p), ('number', C.c_uint64), ('blob', Blob)]


class PropVariant(C.Structure):
    _fields_ = [('vt', C.c_uint16), ('reserved', C.c_uint16 * 3), ('data', VariantData)]


class WindowProperties:
    """Scoped IPropertyStore; strings are freed with PropVariantClear."""
    def __init__(self, hwnd):
        self.store = C.c_void_p()
        user = C.WinDLL('user32')
        user.GetWindowThreadProcessId.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        owner = C.c_uint32()
        user.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if owner.value != os.getpid():
            raise ValueError('Refusing to change a window outside NPK process.')
        self.ole = C.WinDLL('ole32')
        self.ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_uint32]
        self.ole.CoInitializeEx.restype = C.c_long
        hr = self.ole.CoInitializeEx(None, 2)
        self.uninitialize = hr >= 0
        if hr != -2147417850:  # RPC_E_CHANGED_MODE: caller already initialized COM.
            _checked(hr)
        try:
            shell = C.WinDLL('shell32')
            shell.SHGetPropertyStoreForWindow.argtypes = [C.c_void_p, C.POINTER(GUID), C.POINTER(C.c_void_p)]
            shell.SHGetPropertyStoreForWindow.restype = C.c_long
            iid = GUID.from_buffer_copy(uuid.UUID('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99').bytes_le)
            _checked(shell.SHGetPropertyStoreForWindow(hwnd, C.byref(iid), C.byref(self.store)))
            self.props = C.WinDLL('propsys')
            self.props.PSGetPropertyKeyFromName.argtypes = [C.c_wchar_p, C.POINTER(PropertyKey)]
            self.props.PSGetPropertyKeyFromName.restype = C.c_long
            # InitPropVariantFromString is an SDK inline helper, not a DLL
            # export. Allocate the VT_LPWSTR with the COM task allocator.
            self.ole.CoTaskMemAlloc.argtypes = [C.c_size_t]
            self.ole.CoTaskMemAlloc.restype = C.c_void_p
            self.ole.PropVariantClear.argtypes = [C.POINTER(PropVariant)]
        except Exception:
            self.close()
            raise

    def method(self, index, *args):
        vtable = C.cast(self.store, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(C.c_long, C.c_void_p, *args)(vtable[index])

    def key(self, name):
        key = PropertyKey()
        _checked(self.props.PSGetPropertyKeyFromName('System.AppUserModel.' + name, C.byref(key)))
        return key

    def set(self, name, value):
        key, variant = self.key(name), PropVariant()
        try:
            if value is not None:
                encoded = value.encode('utf-16-le') + b'\0\0'
                pointer = self.ole.CoTaskMemAlloc(len(encoded))
                if not pointer:
                    raise MemoryError('Could not allocate icon identity string.')
                variant.vt = 31  # VT_LPWSTR; PropVariantClear frees COM memory.
                variant.data.pointer = pointer
                C.memmove(pointer, encoded, len(encoded))
            _checked(self.method(6, C.POINTER(PropertyKey), C.POINTER(PropVariant))(
                self.store, C.byref(key), C.byref(variant)))
        finally:
            self.ole.PropVariantClear(C.byref(variant))

    def get(self, name):
        key, variant = self.key(name), PropVariant()
        try:
            _checked(self.method(5, C.POINTER(PropertyKey), C.POINTER(PropVariant))(
                self.store, C.byref(key), C.byref(variant)))
            return C.wstring_at(variant.data.pointer) if variant.vt == 31 else None
        finally:
            self.ole.PropVariantClear(C.byref(variant))

    def close(self):
        if self.store:
            self.method(2)(self.store)
            self.store = C.c_void_p()
        if self.uninitialize:
            self.ole.CoUninitialize()
            self.uninitialize = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def identity_properties():
    # Quoted executable avoids interpreting spaces as command boundaries.
    return {'RelaunchCommand': f'"{ROOT / (APP_NAME + ".exe")}"',
            'RelaunchDisplayNameResource': APP_NAME,
            'RelaunchIconResource': f'{ICON},0', 'ID': APP_ID}


def attach_window_identity(window):
    """Call on pywebview shown; marshal native form access to its UI thread."""
    if os.name != 'nt':
        return
    from System import Action
    def install():
        try:
            hwnd = window.native.Handle.ToInt64()
            with WindowProperties(hwnd) as store:
                for key, value in identity_properties().items():
                    store.set(key, value)
            def clear_before_close(sender, event):
                # Registered after pywebview's confirmation handler: do not clear
                # branding when the user cancels closing the window.
                if event.Cancel:
                    return
                try:
                    with WindowProperties(hwnd) as store:
                        for key in identity_properties():
                            store.set(key, None)
                except Exception:
                    logging.getLogger(__name__).warning('Could not clear window identity.', exc_info=True)
            window.native.FormClosing += clear_before_close
        except Exception:
            logging.getLogger(__name__).warning('Could not apply taskbar identity.', exc_info=True)
    window.native.Invoke(Action(install))
