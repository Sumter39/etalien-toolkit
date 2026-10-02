# -*- coding: utf-8 -*-
"""protobuf 的最小实现 —— 只够读我们关心的那几个接口，不引入依赖。

字段号本身不表意：成功响应各接口自定，同名号在不同接口里含义完全不同；
错误响应又是另一套骨架（field1=HTTP 码 / field2=短语 / field3=详情 / field4=服务器秒）。
所以解码结果必须按调用方自己的字段表去读，别套用。
"""


def read_varint(b, i):
    """从 b[i] 起读一个 varint，返回 (值, 新下标)。"""
    n = 0
    shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 溢出")


def decode(b):
    """解出 [(字段号, 类型, 值), ...]，没有 .proto 也能读。

    类型是 varint / bytes / f32 / f64；遇到不认识的 wire type 就停 —— 多半是
    这段不是我们以为的那个消息，继续解只会读出垃圾。
    """
    out = []
    i = 0
    while i < len(b):
        try:
            key, i = read_varint(b, i)
        except ValueError:
            break
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = read_varint(b, i)
            out.append((field, "varint", v))
        elif wire == 2:
            ln, i = read_varint(b, i)
            out.append((field, "bytes", b[i:i + ln]))
            i += ln
        elif wire == 5:
            out.append((field, "f32", b[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, "f64", b[i:i + 8]))
            i += 8
        else:
            break
    return out


def varint(n):
    """把整数编成 varint 字节。"""
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        out.append(x | 0x80 if n else x)
        if not n:
            return bytes(out)


def field(f, n):
    """构造一个 varint 字段（varint 是最常用的类型，够拼请求体用了）。"""
    return varint(f << 3) + varint(n)


def fmt_dur(sec):
    """秒数转人话，如 13时12分2秒。"""
    return f"{sec // 3600}时{sec % 3600 // 60}分{sec % 60}秒"
