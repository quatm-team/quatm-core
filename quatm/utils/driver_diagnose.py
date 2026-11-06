"""Diagnose instrument connection problems.

The command reads addresses from a project ``configuration/device_db.py`` (or
accepts them on the command line), probes VISA/serial/TCP resources and reports
what could be opened. Driver discovery from the optional platform is used when
available; the tool falls back to its own VISA probe otherwise.
"""
import argparse
import sys

from quatm.utils import device_db


def load_discovery():
    """惰性导入专有的 `quatm.drivers.discovery`（不可用时返回 ``None``）。

    为什么惰性：本工具随分发包分发，而 `quatm/drivers/` 既不随包分发、又在
    import 时做许可检查；把导入放在这里并吞掉异常，能保证诊断工具永远可用。

    Returns:
        module|None: discovery 模块；缺包或未授权时为 ``None``。
    """
    try:
        from quatm.drivers import discovery
    except Exception as exc:  # noqa: BLE001 - 诊断工具不能因此起不来
        print('[诊断] 未加载 quatm.drivers.discovery（%s）' % exc)
        print('        探测退回本模块的 pyvisa 实现（仅支持 VISA 地址）。')
        return None
    return discovery


def collect_targets(explicit=()):
    """收集要测试的地址：命令行给的优先，否则读项目 device_db。

    Args:
        explicit (iterable[str]): 命令行传入的地址（可为空）。

    Returns:
        tuple[list[str], dict[str, str]]: 去重保序的地址列表，以及
        ``{地址: 登记它的 device_db 条目名}``（命令行模式下为空字典）。
    """
    if explicit:
        print('[诊断] 使用命令行给出的 %d 个地址。' % len(explicit))
        return list(dict.fromkeys(explicit)), {}
    print('[诊断] 项目根:', device_db.project_root())
    entries, note = device_db.parse_device_db()
    addresses = device_db.addresses(entries)
    counts = device_db.entry_counts(entries)
    print('[诊断] device_db: %s' % note)
    if counts:
        summary = ', '.join('%s=%d' % item for item in sorted(counts.items()))
        print('[诊断] 条目构成: %s' % summary)
    owners = {}
    for entry in entries:
        address = device_db.entry_address(entry)
        if not address:
            continue
        owners[address] = ', '.join(
            filter(None, (owners.get(address), entry.get('name'))))
    if not addresses:
        print('[诊断] device_db 里没有登记地址——老项目的驱动常把配置放在')
        print('        Properties 里；可用 `quatm-driver-diagnose USB0::…` 直接指定。')
    return addresses, owners


def visa_report():
    """打印 pyvisa 后端与系统枚举到的 VISA 资源。

    Returns:
        tuple[object|None, list[str]]: ``(ResourceManager, 资源列表)``；
        pyvisa 缺失或后端不可用时返回 ``(None, [])``。
    """
    try:
        import pyvisa
    except ImportError:
        print('[诊断] 未安装 pyvisa，跳过 VISA 后端信息（pip install pyvisa pyvisa-py）。')
        return None, []
    try:
        manager = pyvisa.ResourceManager()
    except Exception as exc:  # noqa: BLE001
        print('[诊断] 无法创建 VISA ResourceManager: %s' % exc)
        print('        检查是否安装了 VISA 后端（pip install pyvisa-py）或 NI-VISA。')
        return None, []
    print('[诊断] VISA 后端:', type(manager).__module__)
    try:
        resources = [str(item) for item in manager.list_resources()]
    except Exception as exc:  # noqa: BLE001
        print('[诊断] 枚举 VISA 资源失败: %s' % exc)
        return manager, []
    print('[诊断] 系统枚举到 %d 个 VISA 资源:' % len(resources))
    for resource in resources:
        print('        ', resource)
    return manager, resources


def probe_with_discovery(discovery, address):
    """用 `discovery.probe` 探测一个地址（与 GUI/实验同一条 `comm` 路径）。

    Args:
        discovery: 已导入的 discovery 模块。
        address (str): 设备地址（VISA / ``serial://`` / ``tcp://``）。

    Returns:
        tuple[bool, str, str]: ``(是否连上, IDN, 说明)``。
    """
    result = discovery.probe(address)
    detail = result.summary()
    if result.opened and not result.idn:
        detail += '（该设备可能不是 SCPI 设备——能连上就说明传输层没问题）'
    return result.opened, result.idn, detail


def probe_with_pyvisa(manager, address):
    """用 pyvisa 直接探测一个地址（discovery 不可用时的回退路径）。

    Args:
        manager: pyvisa 的 ResourceManager，可能为 ``None``。
        address (str): 设备地址。

    Returns:
        tuple[bool, str, str]: ``(是否连上, IDN, 说明)``。
    """
    if manager is None:
        return False, '', 'pyvisa 不可用，无法探测'
    try:
        instrument = manager.open_resource(address)
    except Exception as exc:  # noqa: BLE001
        return False, '', '打开失败: %s' % exc
    try:
        try:
            idn = str(instrument.query('*IDN?')).strip()
        except Exception:  # noqa: BLE001
            idn = ''
        detail = '打开成功' + (', *IDN? = %s' % idn if idn else '（无法读取 *IDN?）')
        return True, idn, detail
    finally:
        try:
            instrument.close()
        except Exception:  # noqa: BLE001
            pass


def print_hints(address):
    """打印针对一个探测失败地址的排查建议。

    Args:
        address (str): 探测失败的地址。
    """
    text = str(address or '')
    if '::' in text:
        print('          → 资源列表里没有该设备：检查 USB 线/电源/USB 驱动；')
        print('          → 资源在但地址不同：串号变了，用列表里的地址更新 device_db.py。')
    elif text.lower().startswith(('serial://', 'asrl://')) \
            or text.startswith('/dev/') or text.upper().startswith('COM'):
        print('          → 检查串口是否被其它程序占用、波特率是否与设备一致；')
        print('          → 用 `python -m serial.tools.list_ports -v` 确认设备路径。')
    else:
        print('          → 检查网线/交换机、IP 是否可达（ping）、端口是否开放。')


def report(addresses, manager, discovery, owners=None):
    """逐地址探测并打印结论。

    Args:
        addresses (list[str]): 待测地址。
        manager: pyvisa ResourceManager（可为 ``None``）。
        discovery: discovery 模块（可为 ``None``，此时走 pyvisa 回退）。
        owners (dict[str, str]|None): ``{地址: device_db 条目名}``，用于标注归属。

    Returns:
        int: 失败的地址个数。
    """
    owners = owners or {}
    failures = 0
    for address in addresses:
        owner = owners.get(address)
        print('[诊断] 尝试连接 %s%s ...'
              % (address, '（device_db: %s）' % owner if owner else ''))
        if discovery is not None:
            opened, _idn, detail = probe_with_discovery(discovery, address)
        else:
            opened, _idn, detail = probe_with_pyvisa(manager, address)
        if opened:
            print('        ✓ %s' % detail)
            continue
        failures += 1
        print('        ✗ %s' % detail)
        print_hints(address)
    return failures


def main(argv=None):
    """运行诊断：收集地址、打印环境信息、逐地址探测。

    Args:
        argv (list[str]|None): 命令行参数；``None`` 时用 ``sys.argv[1:]``。

    Returns:
        int: 退出码（0 = 全部探测成功；1 = 有失败或没有可测地址）。
    """
    parser = argparse.ArgumentParser(
        description='诊断仪器连接问题（VISA / 串口 / 网络）')
    parser.add_argument('address', nargs='*',
                        help='要测试的地址（缺省则读 configuration/device_db.py）')
    args = parser.parse_args(argv)

    discovery = load_discovery()
    targets, owners = collect_targets(args.address)
    manager, _resources = visa_report()
    if not targets:
        print('[诊断] 没有要测试的地址。')
        return 1
    print()
    print('[诊断] 探测路径:', 'quatm.drivers.discovery.probe（统一路径）'
          if discovery is not None else 'pyvisa（回退，仅 VISA）')
    failures = report(targets, manager, discovery, owners)
    print()
    if failures:
        print('[诊断] 完成：%d/%d 个地址有问题。' % (failures, len(targets)))
        return 1
    print('[诊断] 完成：%d 个地址全部连得上。' % len(targets))
    return 0


if __name__ == '__main__':
    sys.exit(main())
