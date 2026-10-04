"""EXIF 元信息的读取与写回。

平台上传的照片常带有拍摄时间、相机型号、方向、GPS 等隐藏信息（EXIF），
本模块把它们提取成结构化字典供前端展示，并支持按白名单字段修改后写回图像本身。

- read_metadata(path)：读取并结构化；没有任何 EXIF 时 has_exif=False，
  由前端明确提示「该图像不包含元信息」。
- validate_fields(fields)：不落盘地校验一批待修改字段（白名单 + 格式）。
- rewrite_exif(path, fields)：应用修改并返回重新编码后的图像字节，
  由 ImageStore 负责原子落盘、重算哈希与刷新缩略图。

只依赖 Pillow：JPEG 用 quality="keep" 保留原量化表，尽量避免重压缩画质损失；
PNG/WebP/TIFF 直接重存。BMP/GIF 不支持 EXIF，编辑时抛出 ExifError。
"""
import io
from datetime import datetime

from PIL import ExifTags, Image

# 标签名 -> 标签号（0th IFD）
_CODE = {name: code for code, name in ExifTags.TAGS.items()}
# GPS 标签名 -> 标签号
_GPS_CODE = {name: code for code, name in ExifTags.GPSTAGS.items()}

# 支持写回 EXIF 的图像格式
WRITABLE_FORMATS = ("JPEG", "PNG", "WEBP", "TIFF")

# 可编辑字段白名单（批量/单张接口共用）
EDITABLE_FIELDS = ("datetime_original", "orientation", "description",
                   "make", "model", "software")

ORIENTATION_TEXT = {
    1: "正常", 2: "水平翻转", 3: "旋转 180°", 4: "垂直翻转",
    5: "转置（沿对角线翻转）", 6: "顺时针旋转 90°",
    7: "转置并翻转", 8: "逆时针旋转 90°",
}

_DT_INPUT_FORMATS = (
    "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
)

_MAX_TEXT_LEN = 512


class ExifError(ValueError):
    """EXIF 读取/写入中的可预期错误（message 直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_metadata(path):
    """读取图像的 EXIF 并返回结构化字典。无 EXIF 时 has_exif=False。"""
    try:
        img = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ExifError(f"无法打开图像：{exc}")
    fmt = img.format or ""
    exif = img.getexif()
    exif_ifd = _safe_ifd(exif, ExifTags.IFD.Exif)
    gps_ifd = _safe_ifd(exif, ExifTags.IFD.GPSInfo)

    orientation = _to_int(exif.get(_CODE.get("Orientation")))
    return {
        "format": fmt,
        "can_edit": fmt in WRITABLE_FORMATS,
        "has_exif": bool(exif) or bool(exif_ifd) or bool(gps_ifd),
        "tag_count": len(exif) + len(exif_ifd) + len(gps_ifd),
        "capture": {
            "datetime_original": _to_str(exif_ifd.get(0x9003)),
            "datetime_digitized": _to_str(exif_ifd.get(0x9004)),
            "datetime": _to_str(exif.get(_CODE.get("DateTime"))),
        },
        "device": {
            "make": _to_str(exif.get(_CODE.get("Make"))),
            "model": _to_str(exif.get(_CODE.get("Model"))),
            "lens": _to_str(exif_ifd.get(0xA434)),
            "software": _to_str(exif.get(_CODE.get("Software"))),
        },
        "exposure": {
            "iso": _to_int(exif_ifd.get(0x8827)),
            "exposure_time": _fmt_shutter(exif_ifd.get(0x829A)),
            "f_number": _fmt_fnumber(exif_ifd.get(0x829D)),
            "focal_length": _fmt_focal(exif_ifd.get(0x920A)),
            "flash": _to_int(exif_ifd.get(0x9209)),
        },
        "image": {
            "width": img.width,
            "height": img.height,
            "orientation": orientation,
            "orientation_text": ORIENTATION_TEXT.get(orientation) if orientation else None,
            "x_resolution": _to_float(exif.get(_CODE.get("XResolution"))),
            "y_resolution": _to_float(exif.get(_CODE.get("YResolution"))),
        },
        "description": _read_description(exif, exif_ifd),
        "gps": _parse_gps(gps_ifd),
    }


def _read_description(exif, exif_ifd):
    """说明优先取 UserComment（支持中文），退回 ImageDescription（仅 ASCII）。"""
    uc = _decode_user_comment(exif_ifd.get(0x9286))
    if uc:
        return uc
    return _to_str(exif.get(_CODE.get("ImageDescription")))


def _decode_user_comment(raw):
    """按 EXIF 规范的 8 字节字符集头解码 UserComment。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip("\x00 ").strip() or None
    try:
        prefix, body = bytes(raw[:8]), bytes(raw[8:])
    except (TypeError, ValueError):
        return None
    for charset, enc in ((b"ASCII\x00\x00\x00", "ascii"), (b"UNICODE\x00", "utf-16-be")):
        if prefix == charset:
            try:
                return body.decode(enc, "replace").strip("\x00 ").strip() or None
            except Exception:  # noqa: BLE001
                return None
    try:  # 无字符集头：按 UTF-8 尝试
        return bytes(raw).decode("utf-8").strip("\x00 ").strip() or None
    except (UnicodeDecodeError, ValueError):
        return None


def _safe_ifd(exif, ifd_tag):
    """取子 IFD；文件损坏或格式异常时返回空 dict，不让单张图拖垮接口。"""
    try:
        return exif.get_ifd(ifd_tag) or {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_gps(gps_ifd):
    if not gps_ifd:
        return None
    lat = _dms_to_decimal(gps_ifd.get(_GPS_CODE.get("GPSLatitude")),
                          gps_ifd.get(_GPS_CODE.get("GPSLatitudeRef")))
    lon = _dms_to_decimal(gps_ifd.get(_GPS_CODE.get("GPSLongitude")),
                          gps_ifd.get(_GPS_CODE.get("GPSLongitudeRef")))
    alt = _to_float(gps_ifd.get(_GPS_CODE.get("GPSAltitude")))
    if alt is not None and _to_int(gps_ifd.get(_GPS_CODE.get("GPSAltitudeRef"))) == 1:
        alt = -alt
    if lat is None and lon is None and alt is None:
        return None
    return {"latitude": lat, "longitude": lon, "altitude": alt}


def _dms_to_decimal(values, ref):
    """度分秒 -> 十进制度；ref 为 S/W 时取负。"""
    if not values or len(values) < 3:
        return None
    try:
        dec = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if str(ref).strip().upper() in ("S", "W"):
        dec = -dec
    return round(dec, 6)


# ---------------------------------------------------------------------------
# 校验与写回
# ---------------------------------------------------------------------------
def validate_fields(fields):
    """校验一批待修改字段（不触碰文件）。非法时抛 ExifError。"""
    if not isinstance(fields, dict) or not fields:
        raise ExifError("缺少要修改的字段")
    unknown = sorted(set(fields) - set(EDITABLE_FIELDS))
    if unknown:
        raise ExifError(f"不支持的字段：{', '.join(unknown)}（可修改：{', '.join(EDITABLE_FIELDS)}）")
    if fields.get("datetime_original") not in (None, ""):
        _parse_datetime(fields["datetime_original"])
    if fields.get("orientation") not in (None, "", 0):
        _parse_orientation(fields["orientation"])
    for key in ("description", "make", "model", "software"):
        v = fields.get(key)
        if v not in (None, "") and len(str(v)) > _MAX_TEXT_LEN:
            raise ExifError(f"{key} 过长（≤{_MAX_TEXT_LEN} 字符）")


def rewrite_exif(path, fields):
    """按白名单字段修改 EXIF，返回重新编码后的图像字节。

    fields 中出现的 key 表示要修改；值为空字符串/None 表示清除该标签。
    """
    validate_fields(fields)
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ExifError(f"无法打开图像：{exc}")
    fmt = img.format or ""
    if fmt not in WRITABLE_FORMATS:
        raise ExifError(f"{fmt or '该'} 格式不支持写入 EXIF（支持 {'/'.join(WRITABLE_FORMATS)}）")

    exif = img.getexif()
    _apply_fields(exif, fields)

    buf = io.BytesIO()
    kwargs = {"exif": exif}
    if fmt == "JPEG":
        kwargs["quality"] = "keep"  # 保留原量化表，避免二次压缩损失
    try:
        img.save(buf, fmt, **kwargs)
    except Exception as exc:  #  noqa: BLE001
        raise ExifError(f"写回图像失败：{exc}")
    return buf.getvalue()


def _apply_fields(exif, fields):
    """把校验过的字段应用到 Exif 对象上。

    注意：Exif.get_ifd 有内部缓存，子 IFD 必须「取一次、集中改、写回一次」，
    否则后一次赋值会拿旧缓存覆盖前一次的修改。
    """
    sub = dict(exif.get_ifd(ExifTags.IFD.Exif))
    sub_dirty = False

    if "datetime_original" in fields:
        v = fields["datetime_original"]
        if v in (None, ""):
            sub.pop(0x9003, None)   # DateTimeOriginal
            sub.pop(0x9004, None)   # DateTimeDigitized
            exif.pop(_CODE["DateTime"], None)
        else:
            dt = _parse_datetime(v)
            sub[0x9003] = dt
            exif[_CODE["DateTime"]] = dt
        sub_dirty = True

    if "description" in fields:
        v = fields["description"]
        if v in (None, ""):
            exif.pop(_CODE["ImageDescription"], None)
            sub.pop(0x9286, None)   # UserComment
        else:
            text = str(v)
            # ImageDescription 仅 ASCII（规范限制，非 ASCII 会被替换为 ?）；
            # UserComment 以 UNICODE 字符集头保存 UTF-16BE，中文说明靠它保真，
            # 读取时优先取 UserComment。
            exif[_CODE["ImageDescription"]] = text
            try:
                sub[0x9286] = b"ASCII\x00\x00\x00" + text.encode("ascii")
            except UnicodeEncodeError:
                sub[0x9286] = b"UNICODE\x00" + text.encode("utf-16-be")
        sub_dirty = True

    if sub_dirty:
        exif[ExifTags.IFD.Exif] = sub

    if "orientation" in fields:
        v = fields["orientation"]
        if v in (None, "", 0):
            exif.pop(_CODE["Orientation"], None)
        else:
            exif[_CODE["Orientation"]] = _parse_orientation(v)
    for key, tag_name in (("make", "Make"), ("model", "Model"), ("software", "Software")):
        if key in fields:
            v = fields[key]
            if v in (None, ""):
                exif.pop(_CODE[tag_name], None)
            else:
                exif[_CODE[tag_name]] = str(v)


def _parse_datetime(value):
    """接受多种输入格式，统一输出 EXIF 格式 'YYYY:MM:DD HH:MM:SS'。"""
    s = str(value).strip()
    for fmt in _DT_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
    raise ExifError(f"无法识别的拍摄时间「{s}」（示例：2024-05-01 13:30）")


def _parse_orientation(value):
    try:
        o = int(value)
    except (TypeError, ValueError):
        raise ExifError("方向必须是 1-8 的整数")
    if not 1 <= o <= 8:
        raise ExifError("方向必须是 1-8 的整数")
    return o


# ---------------------------------------------------------------------------
# 值格式化辅助
# ---------------------------------------------------------------------------
def _to_str(v):
    if v is None:
        return None
    s = str(v).strip("\x00").strip()
    return s or None


def _to_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _fmt_shutter(v):
    """曝光时间（秒）-> '1/125 s' 或 '2 s'。"""
    if v is None:
        return None
    try:
        sec = float(v)
    except (TypeError, ValueError):
        return None
    if sec <= 0:
        return None
    if sec < 1:
        return f"1/{round(1 / sec)} s"
    return f"{sec:g} s"


def _fmt_fnumber(v):
    f = _to_float(v)
    return f"f/{f:g}" if f else None


def _fmt_focal(v):
    f = _to_float(v)
    return f"{f:g} mm" if f else None
