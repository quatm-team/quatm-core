"""Feshbach-resonance plotting helpers for atom-ion experiments."""

import numpy as np
import pandas
import pandas as pd
import csv
import sys
import pytz
from quatm.physics.odt.utils import wilson_err
from scipy.optimize import curve_fit
from scipy.integrate import odeint
import uncertainties as uc
from os.path import isfile
import h5py
import datetime
from dateutil.tz import tzlocal
from scipy.stats import binned_statistic

# 绘图与 InfluxDB 都改为惰性导入（本模块也常被无显示器的脚本/CI 导入）。
# 详见 quatm/physics/_lazy.py。
from quatm.physics._lazy import lazy_attr, lazy_module
mpl = lazy_module('matplotlib')
plt = lazy_module('matplotlib.pyplot')
mdates = lazy_module('matplotlib.dates')
ticker = lazy_module('matplotlib.ticker')
container = lazy_module('matplotlib.container')
InfluxDBClient = lazy_attr('influxdb_client', 'InfluxDBClient')

# 解析器在 AGPL 的 quatm.servers 里（physics 不能反向依赖可选的 quatm.clients）。
from quatm.servers.influx_config import (InfluxDBConfigError,
                                         resolve_influx_config)
from quatm.utils.i18n import tr

def exp_decay(t, tau, c, A0):
    """指数衰减函数：y = (A0-c)·exp(-t/tau) + c。

    Args:
        t: 时间。tau: 衰减时间常数。c: 基线。A0: 初值。

    Returns:
        衰减曲线值。
    """
    return (A0-c)*np.exp(-(t/tau)) + c

def gaussian_decay(t, A0, tau, c):
    """高斯衰减函数：y = (A0-c)·exp(-(t/tau)²) + c。

    Args:
        t: 时间。A0: 初值。tau: 衰减时间常数。c: 基线。

    Returns:
        高斯衰减曲线值。
    """

def read_h5(fn, dataname, summary, root='/home/bali/Data/Date/'):
    """读取 h5 文件中指定数据集并返回其内容。

    Args:
        fn (str): 文件名。dataname (str): 组名。summary (str): 数据集名。
        root (str): 数据根目录。

    Returns:
        数据集内容（数值/数组）。
    """
    with h5py.File(root+fn) as f:
        for k in f.keys():
            print(k)
            for n in f[k]:
                print('\t', n)
            print()
        return f[dataname][summary][()]

def query_data(field, value, timescale):
    """从 InfluxDB 查询某硬件字段在时间段内的数据。

    连接参数来自运行时配置（configfile ``Servers/InfluxDB`` 的 url/token/org，
    缺的项用环境变量 ``INFLUX_URL`` / ``INFLUX_TOKEN`` / ``INFLUX_ORG`` 补），
    **没有内置凭据**。本模块是一次性绘图脚本：配置缺失时打印可读警告并返回
    ``None``（调用方按“该面板无数据”处理），**绝不回退到假值、绝不打印 token**。

    Args:
        field (str): 硬件名（Hardware tag）。
        value (str): 要查询的字段列。
        timescale (list[datetime]): 1-2 个时间点定义起止。

    Returns:
        DataFrame | None: 查询结果（_time 已转 Europe/Berlin）；缺配置时 None。
    """
    try:
        conf = resolve_influx_config()
    except InfluxDBConfigError as exc:
        print(str(exc), file=sys.stderr)
        print(tr('[InfluxDB] Skipping the InfluxDB data query; '
                 'this panel will be left empty.'), file=sys.stderr)
        return
    client = InfluxDBClient(url=conf['url'], token=conf['token'], org=conf['org'])
    query_api = client.query_api()
    start = ''
    end = 'now()'

    if len(timescale) > 2 or len(timescale) == 0:
        print('Only 1 or two timestamps for defining a time scale!')
        return

    if type(timescale[0]) == str:
        print('Strings for defining timescales are deprecated, only DateTime objects allowed! stopping here ...')
        return

    if len(timescale) == 1:
        start = timescale[0].replace(tzinfo=tzlocal()).astimezone(tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')
    elif len(timescale) == 2:
        start = timescale[0].replace(tzinfo=tzlocal()).astimezone(tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')
        end = timescale[1].replace(tzinfo=tzlocal()).astimezone(tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')

    raw_data = query_api.query_data_frame('from(bucket:"bali")'
                                          '|> range(start: ' + start + ', stop: ' + end + ')'
                                          '|> filter(fn: (r) => r["_measurement"] == "' + 'lab_status' + '")'
                                          '|> filter(fn: (r) => r["Hardware"] == "' + field + '")'
                                          '|> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
                                          '|> keep(columns: ["_time", "' + value + '"])')

    raw_data._time = raw_data._time.dt.tz_convert('Europe/Berlin')

    return raw_data

def filter_data_by_time(data, timescale):
    """按时间范围过滤 InfluxDB 数据帧。

    Args:
        data: 数据帧（含 _time 列）。
        timescale (list[datetime]): 一个时间点为下界，两个时间点定区间。

    Returns:
        过滤后的数据帧。
    """
    if len(timescale) == 1:
        return data[data._time >= timescale[0]]
    elif len(timescale) == 2:
        return data[(data._time >= min(timescale)) & (data._time <= max(timescale))]
    else:
        print("No more than three timestamps for defining time scale!")

def load_data(fn, full_output=False, delim=None):
    """从计数表加载数据并预计算存活概率与 Wilson 误差。

    默认列：B[G]  N_surv  N_det  N_d52  N_loss（可指定分隔符/列名）。

    Args:
        fn (str): 数据文件名（data/ 下）。
        full_output (bool): True 时同时返回全部计数列。
        delim: 分隔符（None 自动）。

    Returns:
        (B, P_surv, err_lower, err_upper[, N_tot, N_inel, N_surv, N_det, N_d52, N_loss])。
    """
    colnames = ['B[G]', 'N_surv', 'N_det', 'N_d52', 'N_loss']

    if fn[-3:] == 'csv':
        csv_colnames = ['B[G]', 'N_invalid', 'N_surv', 'N_det', 'N_d52',
                        'N_loss']  # ['Unnamed: 0', '1.0', '2.0', '3.0', '4.0']
        df = pd.read_csv('data/' + fn, delimiter='\t', names=csv_colnames)
        # for on, nn in zip(csv_colnames, colnames):
        # df.rename(columns={on:nn}, inplace=True)
        # print(df)

    else:
        df = pd.read_csv('data/' + fn, delim_whitespace=True)

    df.fillna(0)
    df = df.groupby(colnames[0]).sum().reset_index()

    B = df[colnames[0]]
    N_surv = df[colnames[1]]
    N_det = df[colnames[2]]
    N_d52 = df[colnames[3]]
    N_loss = df[colnames[4]]

    # inelastic losses and total events
    N_inel = N_det + N_d52 + N_loss
    N_tot = N_inel + N_surv

    # calculate wilson score intervals
    P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
    err_lower = np.where(np.isclose(err_lower, 0), 0, err_lower)
    err_upper = np.where(np.isclose(err_upper, 0), 0, err_upper)

    if full_output:
        return B, P_surv, err_lower, err_upper, N_tot, N_inel, N_surv, N_det, N_d52, N_loss
    else:
        return B, P_surv, err_lower, err_upper

def convert_event_file(filename):
    """把事件计数文件转换为标准 B[G]/N_xxx 制表文件并保存。

    Args:
        filename (str): data/ 下的事件文件名。
    """
    with open ('data/'+filename) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter='\t')
        B_vals = []
        results = []
        r = 0
        d = {}
        for row in csv_reader:
            if r == 0:
                r += 1
            else:
                B_vals.append(row[0])
                results.append([float(i) if i!='' else 0 for i in row[1:]])
        results=np.array(results)
        if len(results[0]) == 5:
            results[:,-1] = results[:,-1]+results[:,0]
            results = results[:,1:]
        df = pd.DataFrame(results, columns = ['N_surv','N_det','N_d52','N_loss'], index=B_vals)
        df.index.name = 'B[G]'
        print('to csv')
        df.to_csv('data/'+filename[:-4]+'_conv.txt', sep='\t')

def load_rate_measurement_data(fn, tww_key='tww[ms]', B_key='B[G]'):
    """加载速率测量文件（tww×B），按 B 分组计算存活概率。

    文件列：tww[ms]  B[G]  N_surv  N_det  N_d52  N_loss。

    Args:
        fn (str): 文件路径。tww_key: tww 列名。B_key: 磁场列名。

    Returns:
        dict: {B: [tww, P_surv, err_lower, err_upper]}。
    """
    df = pd.read_csv(fn, delim_whitespace=True)
    result = {}
    for B in df[B_key].unique():
        d = df[df[B_key]==B]
        tww = d[tww_key]
        N_surv = d['N_surv']
        N_det = d['N_det']
        N_d52 = d['N_d52']
        N_loss = d['N_loss']
        
        # inelastic losses and total events
        N_inel = N_det + N_d52 + N_loss
        N_tot = N_inel + N_surv
        
        P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
        result[str(B)] = [tww, P_surv, err_lower, err_upper]
        
    return result

def load_rate_measurement_data_tem(fn, tww_key='tww[ms]', T_key='T[uK]'):
    """加载速率测量文件（tww×T），按温度分组计算存活概率。

    文件列：tww[ms]  T[uK]  N_surv  N_det  N_d52  N_loss。

    Args:
        fn (str): 文件路径。tww_key: tww 列名。T_key: 温度列名。

    Returns:
        dict: {T: [tww, P_surv, err_lower, err_upper]}。
    """
    df = pd.read_csv(fn, delim_whitespace=True)
    result = {}
    for T in df[T_key].unique():
        d = df[df[T_key]==T]
        tww = d[tww_key]
        N_surv = d['N_surv']
        N_det = d['N_det']
        N_d52 = d['N_d52']
        N_loss = d['N_loss']
        
        # inelastic losses and total events
        N_inel = N_det + N_d52 + N_loss
        N_tot = N_inel + N_surv
        
        P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
        result[str(T)] = [tww, P_surv, err_lower, err_upper]
        
    return result

def load_power_measurement_data(fn, P_key='P[W]', B_key='B[G]'):
    """加载功率测量文件（P×B），按 B 分组计算存活概率。

    文件列：P[W]  B[G]  N_surv  N_det  N_d52  N_loss。

    Args:
        fn (str): 文件路径。P_key: 功率列名。B_key: 磁场列名。

    Returns:
        dict: {B: [P, P_surv, err_lower, err_upper]}。
    """
    df = pd.read_csv(fn, delim_whitespace=True)
    result = {}
    for B in df[B_key].unique():
        d = df[df[B_key]==B]
        tww = d[P_key]
        N_surv = d['N_surv']
        N_det = d['N_det']
        N_d52 = d['N_d52']
        N_loss = d['N_loss']
        
        # inelastic losses and total events
        N_inel = N_det + N_d52 + N_loss
        N_tot = N_inel + N_surv
        
        P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
        result[str(B)] = [tww, P_surv, err_lower, err_upper]
        
    return result

def load_arbitrary_data_combined(fns, groups):
    """加载多个数据文件并按指定分组求和合并，计算存活概率。

    Args:
        fns (list[str]): 数据文件路径列表。
        groups (list[str]): 分组列（首列将作为未来 x 轴）。

    Returns:
        pandas.DataFrame: 合并并含 P_surv/err 列的结果。
    """
    
    df1 = pd.read_csv(fns[0], delim_whitespace=True)
    dfc = df1.groupby(groups).sum().reset_index()
    
    # iteratively combine the new dataframes into the initial one
    if len(fns) > 1:
        for fn in fns[1:]:
            df2 = pd.read_csv(fn, delim_whitespace=True)
            dfc = dfc.groupby(groups).sum().add(df2.groupby(groups).sum(), fill_value=0).reset_index()


    result = pd.DataFrame(columns=np.concatenate((groups, ['N_surv', 'N_det', 'N_d52', 'N_loss'])))
    
    for index, row in dfc.iterrows():
        N_surv = row['N_surv']
        N_det = row['N_det']
        N_d52 = row['N_d52']
        N_loss = row['N_loss']
    
        # inelastic losses and total events
        N_inel = N_det + N_d52 + N_loss
        N_tot = N_inel + N_surv

        if N_tot == 0:
            continue
        P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
        
        err_lower = min(err_lower, 1)
        err_lower = max(err_lower, 0)
        err_upper = min(err_upper, 1)
        err_upper = max(err_upper, 0)
        
        row['P_surv'] = P_surv
        row['err_lower'] = err_lower
        row['err_upper'] = err_upper
        
        row = pd.DataFrame([row])
        result = pd.concat([result, row], axis=0, ignore_index=True)
    return result

def save_h5_as_csv(data, filename, independent_variable='B_imaging', dependent_variables=[], feshbach_counts=True,
                   feshbach_prob=False,
                   data_filters={}, verbose_output=False, overwrite_files=False, rename_columns={}, include_count=False,
                   split_files=[], point_representation='p', create_csv=True, create_txt=True,
                   csv_root='/home/bali/data/Date/', txt_root='data/', only_warnings=False):
    """
        This function accepts pandas DataFrames (like the h5 content), converts the data and saves it using a specified format as txt/csv file.

        The txt file can be designed by dependent variables and column names, while the csv file has a fixed format:
            -> csv header = ['', '0', '1', '2', '3', '4'] standing for ['B[G], 'N_surv', 'N_det', 'N_d52', 'N_loss']

        Filtering takes place before renaming columns!

        Args:
            data: Pandas DataFrame
            filename: str, name of the file without path or ending
            independent_variable: str, the name of the variable of the first column
            dependent_variables: [str], all variable names which should be present in the save files
            feshbach_counts: bool, if True ['N_surv', 'N_det', 'N_d52', 'N_loss'] will be filled using data['ion_state_ionchecklist.result']. There will be no csv file when not feshbach_counts!
            feshbach_prob: bool, if True ['P_surv', 'err_lower', 'err_upper'] will be filled using data['ion_state_ionchecklist.result']. This won't be part of the csv file!
            data_filters: dict, filter out data which doesn't fulfill conditionals, e.g., by data_filters={'B_imaging': ['>0', '==321.8'], 'interaction_offset_voltage_x': ['== 0']}
            verbose_output: bool, whether to print details of the computation
            overwrite_files: bool, iff True files are allowed to be overwritten
            rename_columns: dict, e.g., rename_columns={'B_imaging':'B[G]', 'interaction_offset_voltage_x':'dx'}
            include_count: bool, whether to create a column at the end for the number of duplicates of the rows
            split_files: [str], for each combination of variables specified, a new file will be created, e.g., split_files=['interaction_offset_voltage_x', 'interaction_offset_voltage_y']
            point_representation: str, how to write numbers in file names, e.g., '2023_05_25_test_0.7µK.txt' -> '2023_05_25_test_0p7µK.txt'
            create_csv: bool, whether to write the csv file as specified above (csv header = ['', '0', '1', '2', '3', '4'])
            create_txt: bool, whether to write the txt file specified using dependent_variables
            csv_root: str, the path to the csv file, standard is '/home/bali/data/Date/'
            txt_root: str, the path to the txt file, standard is 'data/'
            only_warnings: bool, whether to print warnings and errors but no low-level info

        Returns:
            data: Pandas DataFrame, the data which will be stored before distributing it onto several files
    """

    data = pd.DataFrame(data)
    if len(dependent_variables) == 0:
        columns_to_drop = np.array([dc for dc in split_files if dc != independent_variable])
    else:
        columns_to_drop = np.array(
            [dc for dc in split_files if dc not in dependent_variables and dc != independent_variable])
    dependent_variables = np.unique(np.append(dependent_variables, split_files))
    if verbose_output and not only_warnings:
        print('Using columns {0} for calc, will be dropped.\n'.format(columns_to_drop))

    # Filter the data by data_filters:
    for filter_column in data_filters:
        for expression in data_filters[filter_column]:
            data.query('`' + filter_column + '`' + expression, inplace=True)
            if verbose_output and not only_warnings:
                print('\033[1mFilter \'{0} {1}\' \033[0m\t=> {0} in {2}'.format(filter_column, expression,
                                                                                data[filter_column].unique()))

    if verbose_output and not only_warnings:
        print()

    # Extract the columns out of the dataset defined by (in)dependent_variables:
    header = np.unique(np.append(independent_variable, dependent_variables))

    # Check whether there are other params varied in the dataset which aren't specified by the user:
    rvalues = ['abs_h_', 'gaussx', 'gaussy', 'Ion_Countbloblist', 'ion_state_ionchecklist', 'rowsum', 'colsum', 'stray_field_calc']
    for col in data.columns:
        if len(data[col].dropna().unique()) > 1 and col not in header and col not in split_files and col[0] != '_':
            found = False
            for rval in rvalues:
                if rval in col:
                    found = True
            if not found:
                print('\tWarning: The dataframe differs in the non-specified column', col, ':', data[col].dropna().unique())

    # Interprete ion_state_ionchecklist.result as Feshbach counts:
    if feshbach_counts or feshbach_prob:
        header_addon_counts = ['N_surv', 'N_det', 'N_d52', 'N_loss']
        header_full = np.unique(np.append(header, header_addon_counts))
        for enum, addon in enumerate(header_addon_counts):
            data[addon] = data['ion_state_ionchecklist.result'] == (enum + 1)

        data[header_addon_counts] = data[header_addon_counts].astype('int64')
    else:
        header_full = header

    data = data[header_full]

    # Sum up the Feshbach tally chart:
    if verbose_output and not only_warnings:
        print('Summing up:\n\theader = {0}\n\theader_addon_counts = {1}'.format(header,
                                                                                header_addon_counts if feshbach_counts else None))
    if feshbach_counts or feshbach_prob:
        count = data.groupby(list(header))[header_addon_counts].count()[header_addon_counts[-1]].values
        data = data.groupby(list(header))[header_addon_counts].sum().reset_index()
        if include_count:
            data['count'] = count
            header_full = np.unique(np.append(header_full, ['count']))
    else:
        data = data.groupby(list(header))[header[-1]].agg(['count']).reset_index()
        if not include_count:
            data = data.drop(columns='count')

    # Calculate Feshbach survival probabilities including Wilson score:
    if feshbach_prob:
        n_surv = data['N_surv']
        n_tot = data['N_surv'] + data['N_det'] + data['N_d52'] + data['N_loss']
        prob, l_err, u_err = wilson_err(n_tot, n_surv)
        data['P_surv'] = prob
        data['err_lower'] = l_err
        data['err_upper'] = u_err

        header_addon_prob = ['P_surv', 'err_lower', 'err_upper']
        header_full = np.unique(np.append(header_full, header_addon_prob))
        data[header_addon_prob] = data[header_addon_prob].astype('float')

        if verbose_output and not only_warnings:
            print('Summing up probabilities:\n\theader = {0}'.format(header_full))

        if not feshbach_counts:
            data = data.drop(columns=header_addon_counts)

    # Rename columns:
    if verbose_output and not only_warnings:
        print('Renaming:\n\trename_columns = {0}\n\tsplit_files = {1}\n\tcolumns_to_drop = {2}'.format(rename_columns,
                                                                                                       split_files,
                                                                                                       columns_to_drop))
    data.rename(columns=rename_columns, inplace=True)
    for col in rename_columns:
        split_files = [elt if elt != col else rename_columns[col] for elt in split_files]
        columns_to_drop = [elt if elt != col else rename_columns[col] for elt in columns_to_drop]
    if verbose_output and not only_warnings:
        print('to:\n\tsplit_files = {0}\n\tcolumns_to_drop = {1}'.format(split_files, columns_to_drop))

    # Save the data to the files:

    # Recursively create files defined by split_files:
    _recursive_h5_save(split_files, data, filename, csv_root, txt_root, overwrite_files, feshbach_counts,
                       point_representation, columns_to_drop, create_csv=create_csv, create_txt=create_txt, only_warnings=only_warnings)

    return data

def _recursive_h5_save(split_files, data, filename, csv_root, txt_root, overwrite_files, feshbach_counts,
                       point_representation, columns_to_drop, path_addon='', create_csv=True, create_txt=True, only_warnings=False):
    """
        Recursively iterate all combinations of split_files, filter the data frame and save the data to txt/csv files.

        Example:
                split_files=['interaction_offset_voltage_y', 'MOT_loading']
                data['interaction_offset_voltage_y'] in [0, 0.01, 0.03, 0.04]
                data['MOT_loading'] in [1, 3]

                Then, the first file will contain data where data['interaction_offset_voltage_y'] == 0 and data['MOT_loading'] == 1
                    and will be named filename_interaction_offset_voltage_y_0_MOT_loading_1.txt/csv.

        Args:
            split_files: [str], column names which define the file differences (see above)
            data: Pandas DataFrame, the data which will be filtered and distributed over all files
            filename: str, the name you can give the file, see above
            csv_root: str, where the csv file will be located at
            txt_root: str, where the txt file will be located at
            overwrite_files: bool, iff True files are allowed to be overwritten
            feshbach_counts: bool, if True ['N_surv', 'N_det', 'N_d52', 'N_loss'] will be filled using data['ion_state_ionchecklist.result']. There will be no csv file when not feshbach_counts!
            point_representation: str, how to write numbers in file names, e.g., '2023_05_25_test_0.7µK.txt' -> '2023_05_25_test_0p7µK.txt'
            columns_to_drop: [str], column names which need to be tracked for splitting data into files but aren't specified by dependent_variables of save_h5_as_csv, hence, shall show up in the files
            path_addon: str, recursively filled file name, containing the variable names and contents created by split_files
            create_csv: bool, whether to write the csv file as specified above (csv header = ['', '0', '1', '2', '3', '4'])
            create_txt: bool, whether to write the txt file specified using dependent_variables
            only_warnings: bool, whether to print no low-level info

        Returns:
            --
    """

    if len(split_files) > 0:
        for split_var_content in data[split_files[0]].unique():
            path_addon_new = '_{0}_{1}'.format(split_files[0],
                                               str(split_var_content).replace('.', point_representation))
            data_to_save = data.copy()
            data_to_save = data_to_save[data_to_save[split_files[0]] == split_var_content]
            if len(split_files) > 1:
                _recursive_h5_save(split_files[1:], data_to_save, filename, csv_root, txt_root, overwrite_files,
                                   feshbach_counts, point_representation, columns_to_drop,
                                   path_addon=path_addon + path_addon_new, create_csv=create_csv, create_txt=create_txt, only_warnings=only_warnings)
            else:
                _h5_save(data_to_save, csv_root + filename + path_addon + path_addon_new + '.csv',
                         txt_root + filename + path_addon + path_addon_new + '.txt', overwrite_files=overwrite_files,
                         feshbach_counts=feshbach_counts, columns_to_drop=columns_to_drop, create_csv=create_csv,
                         create_txt=create_txt, only_warnings=only_warnings)
    else:
        _h5_save(data, csv_root + filename + path_addon + '.csv', txt_root + filename + path_addon + '.txt',
                 overwrite_files=overwrite_files, feshbach_counts=feshbach_counts, columns_to_drop=columns_to_drop,
                 create_csv=create_csv, create_txt=create_txt, only_warnings=only_warnings)

def save_analysis_files(data, fn_csv, data_filters, dependent_variables, independent_variable='B_imaging',
                        evaluate_feshbach=False, overwrite_files=False, post_selection=[],
                        time_window_resolution=False, drift_data=False, verbose_output=False, create_csv=False,
                        data_filters_atoms={'B_imaging': ['==345']}, stray_field_script=False):
    """生成用于分析 Feshbach 数据的多种文件（含/不含时间窗与漂移数据）。

    Args:
        data: 数据帧。fn_csv: 输出主文件。data_filters: 数据过滤条件。
        dependent_variables: 因变量列。independent_variable: 自变量列。
        evaluate_feshbach (bool): 是否评估 Feshbach 计数。
        overwrite_files/post_selection/time_window_resolution/drift_data/
        verbose_output/create_csv/data_filters_atoms/stray_field_script:
            各功能开关与附加参数。

    Returns:
        处理后的数据帧。
    """
    """
    This function creates a load of files to analyze Feshbach data with/without time-window resolution, as well as drifts of ions/atoms.

    Files to be stored:
        - fn_csv: Feshbach counts without time-window cropped data
        - fn_csv + '_evaluated': Feshbach probability without time-window cropped data
        - fn_csv + '_post_selected': Feshbach counts with time-window cropped data
        - fn_csv + '_post_selected' + '_evaluated': Feshbach probability with time-window cropped data
        - fn_csv + '_hour_{x}': Feshbach counts cropped to 1 hour of measurement
        - fn_csv + '_atom_drifts': Drift data of the atoms (xODT position, atom number)
        - fn_csv + '_ion_drifts': Drift data of the ion (position)
        - data_filters_atoms: The filters only for the atom number / xODT position drifts

    data: pd.DataFrame, all the h5 data to be sorted into files
    fn_csv: str, filename of the stored files
    data_filters: for filtering out invalid data, see doc string of save_h5_as_csv()
    dependent_variables: [str], all variables needed, see save_h5_as_csv()
    independent_variable (opt): str, the variable which aill always be on the x-axis, see doc string of save_h5_as_csv()
    evaluate_feshbach (opt): bool, whether to create the '_evaluated' files above
    overwrite_files (opt): bool, whether to overwrite any files
    time_window_resolution (opt): bool, whether to create the '_hour_{x}' files above
    drift_data (opt): bool, whether to create the '_atom_drifts' / '_ion_drifts' files above
    verbose_output (opt): bool, whether to print a load of information about the analysis
    create_csv (opt): bool, whether to create csv files according to save_h5_as_csv()
    returns: int number_hours, the duration of the experiment
    """

    data = pd.DataFrame(data)

    # Save pure Feshbach data:
    save_h5_as_csv(data, fn_csv, independent_variable=independent_variable, dependent_variables=dependent_variables,
                   data_filters=data_filters,
                   feshbach_counts=True, feshbach_prob=False, verbose_output=verbose_output,
                   overwrite_files=overwrite_files, create_csv=create_csv)
    if evaluate_feshbach:
        save_h5_as_csv(data, fn_csv + '_evaluated', independent_variable=independent_variable,
                       dependent_variables=dependent_variables, data_filters=data_filters,
                       feshbach_counts=False, feshbach_prob=True, verbose_output=verbose_output,
                       overwrite_files=overwrite_files, create_csv=create_csv)

    # Save pure Feshbach data including post-selection:
    if len(post_selection) == 2:
        post_selection_unix = dateTime_to_unix(post_selection)
        save_h5_as_csv(data, fn_csv + '_post_selected', independent_variable=independent_variable,
                       dependent_variables=dependent_variables,
                       data_filters={**data_filters, **{
                           '_starttime': ['>' + str(post_selection_unix[0]), '<' + str(post_selection_unix[-1])]}},
                       feshbach_counts=True, feshbach_prob=False, verbose_output=verbose_output,
                       overwrite_files=overwrite_files, create_csv=create_csv)
        if evaluate_feshbach:
            save_h5_as_csv(data, fn_csv + '_post_selected_evaluated', independent_variable=independent_variable,
                           dependent_variables=dependent_variables,
                           data_filters={**data_filters, **{
                               '_starttime': ['>' + str(post_selection_unix[0]), '<' + str(post_selection_unix[-1])]}},
                           feshbach_counts=False, feshbach_prob=True, verbose_output=verbose_output,
                           overwrite_files=overwrite_files, create_csv=create_csv)

    number_hours = -1
    scan_time_window = [min(data['_starttime']), max(data['_starttime'])]
    number_hours = (int)(np.ceil(np.diff(scan_time_window)[0] / 3600))

    # Save pure Feshbach data for different time windows:
    if time_window_resolution:
        dots_printed = False
        for hour in range(1, number_hours):
            end_time = scan_time_window[0] + 3600 * hour
            less_verbose = hour > 1 and hour < number_hours - 1 and hour % 10 != 0
            save_h5_as_csv(data, fn_csv + '_hour_{0}'.format(int(hour)), independent_variable=independent_variable,
                           dependent_variables=dependent_variables,
                           data_filters={**data_filters,
                                         **{'_starttime': ['<' + str(end_time), '>=' + str(end_time - 3600)]}},
                           feshbach_counts=True, feshbach_prob=False, verbose_output=verbose_output,
                           overwrite_files=overwrite_files, create_csv=False, only_warnings=less_verbose)
            if less_verbose and not dots_printed:
                print('...')
                dots_printed = True
            elif not less_verbose:
                dots_printed = False

    # Save corresponding drift data (position / atom number):
    save_h5_as_csv(data, fn_csv + '_atom_drifts', independent_variable=independent_variable,
                   dependent_variables=['eva_ramp_target_p', 'abs_h_cutrowintcut.abs_h_gaussy.A0',
                                        'abs_h_cutcolintcut.abs_h_gaussx.pos', '_starttime'],
                   data_filters=data_filters_atoms, feshbach_counts=False, feshbach_prob=False,
                   verbose_output=verbose_output, overwrite_files=overwrite_files, create_csv=False)

    """
    Default:
        B_imaging ==0                           # get rid of interaction experiment
        Ion_Countbloblist_.N_bright == 1
    
    Optionally later:
        Nr_Ablation_Pulses == 0                 # get rid of loading experiment
        opt_ramp_duration == 0                  # get rid of optical trapping experiment
        n_trigs == 0                            # get rid of trigger_cam experiment
        cool_power == 0                         # get rid of pumpi experiment
        cooling_power == 0                      # get rid of compensation helper experiment
    """

    if stray_field_script:
        save_h5_as_csv(data, fn_csv + '_stray_field_drifts', independent_variable=independent_variable,
                       data_filters={'Ion_Countbloblist_.N_bright': ['==1'], 'rf_set': ['>0'], 'cooling_power': ['>0'], 'stray_field_calc.ax_yrowsumcut.axgaussy.stray_field': ['!=0']},
                       dependent_variables=['_starttime', 'stray_field_calc.ax_yrowsumcut.axgaussy.position_shift',
                                            'stray_field_calc.ax_yrowsumcut.axgaussy.stray_field',
                                            'stray_field_calc.axcolsumcut.axgaussx.position_shift',
                                            'stray_field_calc.axcolsumcut.axgaussx.stray_field',
                                            'stray_field_calc.radba_xcolsumcut.radba_gaussx_skewed.position_shift',
                                            'stray_field_calc.radba_xcolsumcut.radba_gaussx_skewed.stray_field'],
                       feshbach_counts=False, feshbach_prob=False, verbose_output=verbose_output,
                       overwrite_files=overwrite_files, create_csv=False)

        save_h5_as_csv(data, fn_csv + '_ion_drifts', independent_variable=independent_variable,
                       data_filters={'Ion_Countbloblist_.N_bright': ['==1'], 'B_imaging': ['==0']},
                       dependent_variables=['radba_xcolsumcut.radba_gaussx_skewed.pos', 'radba_yrowsumcut.radba_gaussy.pos',
                                            '_starttime', 'Nr_Ablation_Pulses', 'opt_ramp_duration', 'n_trigs', 'cool_power', 'cooling_power'],
                       feshbach_counts=False, feshbach_prob=False, verbose_output=verbose_output,
                       overwrite_files=overwrite_files, create_csv=False)

    return number_hours

def _h5_save(data, csv_path, txt_path, overwrite_files=False, feshbach_counts=True, columns_to_drop=[],
             create_csv=True, create_txt=True, only_warnings=False):
    """
        The actual saving function. Here, data will be dumped into files already fully specified in csv_path/txt_path after getting rid of columns_to_drop.

        Args:
            data: Pandas DataFrame, the data which will be stored without columns_to_drop
            csv_path: str, the full path/filename
            txt_path: str, the full path/filename
            overwrite_files: bool, iff True files are allowed to be overwritten
            feshbach_counts: bool, if True ['N_surv', 'N_det', 'N_d52', 'N_loss'] will be filled using data['ion_state_ionchecklist.result']. There will be no csv file when not feshbach_counts!
            columns_to_drop: [str], column names which need to be tracked for splitting data into files but aren't specified by dependent_variables of save_h5_as_csv, hence, shall show up in the files
            create_csv: bool, whether to write the csv file as specified above (csv header = ['', '0', '1', '2', '3', '4'])
            create_txt: bool, whether to write the txt file specified using dependent_variables
            only_warnings: bool, whether to print no low-level info

        Returns:
            --
    """

    csv_exists = False
    txt_exists = False

    data = data.drop(columns=columns_to_drop)

    if isfile(csv_path):
        if not only_warnings:
            print('The csv file already exists:', csv_path)
        csv_exists = True
    if isfile(txt_path):
        if not only_warnings:
            print('The txt file already exists:', txt_path)
        txt_exists = True
    if txt_exists or csv_exists:
        if not only_warnings:
            print()

    if min(len(csv_path), len(txt_path)) < 5:
        print('\n\033[1mGive the files a proper name!\n\tAborting to save...\033[0m\n')
    else:
        data_to_save = data.copy()
        if (overwrite_files or not txt_exists) and create_txt:
            data_to_save.to_csv(txt_path, sep=' ', index=False)
            if not only_warnings:
                print('Created txt file', txt_path, ('' if feshbach_counts else '\n'))

        if feshbach_counts and (overwrite_files or not csv_exists) and create_csv:
            header_csv = data_to_save.columns
            if 'B[G]' not in header_csv:
                if 'B_imaging' in header_csv:
                    data_to_save.rename(columns={'B_imaging': 'B[G]'}, inplace=True)
                else:
                    print('_h5_save: Couldn\'t save csv file, neither \'B[G]\' nor \'B_imaging\' in header.')
                    return
            data_to_save.rename(columns={'B[G]': '', 'N_surv': '1', 'N_det': '2', 'N_d52': '3', 'N_loss': '4'},
                                inplace=True)
            data_to_save['0'] = 0
            data_to_save = data_to_save[['', '0', '1', '2', '3', '4']]
            data_to_save.to_csv(csv_path, sep='\t', index=False)
            if not only_warnings:
                print('Created csv file', csv_path, '\n')

def recursive_plot_groups(fig, axe, dataframe, groups, group_index=1, label='', create_label=True, show_errorbars=True, **kwargs):
    """按分组层级递归地把数据绘制到坐标轴上（用于多级分组图）。

    Args:
        fig/axe: matplotlib 图形与坐标轴。
        dataframe: 数据表。groups: 分组列列表。
        group_index: 当前分组层级。
        label: 图例标签。create_label (bool): 是否自动生成标签。
        show_errorbars (bool): 是否显示误差棒。
        **kwargs: 传给绘图函数的参数。
    """
    lines = np.array([], dtype=container.ErrorbarContainer)

    if len(groups) == 1:
        line = axe.errorbar(dataframe[groups[0]], dataframe['P_surv'], yerr=[dataframe['err_lower'], dataframe['err_upper']] if show_errorbars else None,
                     marker='.', elinewidth=.6 if show_errorbars else 0, capthick=.6 if show_errorbars else 0, label=label, **kwargs)
        lines = np.append(lines, line)
    else:
        for group_val in dataframe[groups[group_index]].unique():
            new_label = label + ('' if label == '' else ', ') + str(groups[group_index]) + ': ' + str(group_val)
            select = dataframe[groups[group_index]] == group_val
            if group_index < len(groups) - 1:
                recursive_plot_groups(fig, axe, dataframe[select], groups, group_index=group_index + 1, label=new_label, show_errorbars=show_errorbars)
            else:
                line = axe.errorbar(dataframe[select][groups[0]], dataframe[select]['P_surv'], yerr=[dataframe[select]['err_lower'], dataframe[select]['err_upper']] if show_errorbars else None,
                             marker='.', elinewidth=.6 if show_errorbars else 0, capthick=.6 if show_errorbars else 0, label=new_label, **kwargs)
                lines = np.append(lines, line)

    return lines

def plot_arbitrary_columns(dataframe, groups, figaxe=None, preliminary=False, show_errorbars=True, **kwargs):
    """按分组绘制数据表中任意列的存活概率与误差。

    Args:
        dataframe: 数据表。groups: 分组列。
        figaxe: (fig, axe)；None 新建。
        preliminary (bool): 是否加水印。
        show_errorbars (bool): 是否显示误差棒。
        **kwargs: 绘图参数。
    """
    '''
    A small helper to plot a quick preview of counting experiment results.
    
    :figaxe: tuple
        (fig, axe) to which the given data should be added
        
    :preliminary: bool
        If True, add big PRELIMINARY disclaimers to the plot in order to warn Tobias.
    '''
    
    if 'linestyle' not in kwargs.keys():
        kwargs['linestyle'] = 'none'
    if figaxe is None:
        fig, axe = plt.subplots()
    else:
        fig, axe = figaxe
    
    
    lines = recursive_plot_groups(fig, axe, dataframe, groups, show_errorbars=show_errorbars)
        
    axe.set_xlabel(groups[0])
    axe.set_ylabel(r'P$_{\rm surv}$')
    axe.set_ylim(bottom=-0.1, top=1.1)
    axe.legend()
    axe.grid()
    
    if preliminary:
        for x in np.linspace(0.2, .8, 4):
            axe.text(x, 1-x, 'PRELIMINARY', transform=axe.transAxes,
            fontsize=60, color='gray', alpha=0.1,
            ha='center', va='center', rotation='30')
            
    fig.tight_layout()
    
    return fig, axe, lines

def drift_plot(fn_csv, download_influx_data=False, filtering=False, offsetting=False, ion_filters={}, stray_filters={}, atom_filters={},
               verbose_output=False, conversion_peva_t_li=4.58, raw_data_vac=None, raw_data_temp=None, influx_data=None, influx_columns=None,
               stray_field_script=False, binningsubplots={}):
    """
    This function will read already stored analysis files (see save_analysis_files()) and plot ion/atom/influx drift data.

    :param fn_csv: str, file name as given to save_analysis_files(), must start with date 'yyyy_MM_dd'
    :param download_influx_data: bool, whether to download from influxDB or use downloaded data raw_data_vac, raw_data_temp
    :param filtering: bool, whether to apply ion_filters, atom_filters
    :param offsetting: bool, whether to center ion/atoms position data around 0
    :param ion_filters: analogous to filters in save_h5_as_csv()
    :param atom_filters: analogous to filters in save_h5_as_csv()
    :param verbose_output: bool, whether to print filter results
    :param conversion_peva_t_li: float, conversion factor between p_eva[%] and T_Li [µK]
    :param raw_data_vac: pd.DataFrame, influxDB data for the ion gauge
    :param raw_data_temp: pd.DataFrame, influxDB data for the chamber temperature
    :param influx_columns: dict,
        {col0: {title: 'Main chamber ion gauge', hardware: 'Vaccum_main', field: 'vacuum_degree',
                ylabel: r'$p~$[1e-10 mbar]', multiplier: 1e10, scatter: False},
        col1: ...}
    :return: fig, axe, *(raw_data_vac, raw_data_temp)  or  fig, axe, influx_data
    """

    date = fn_csv.split('_')[0] + '_' + fn_csv.split('_')[1] + '_' + fn_csv.split('_')[2]

    data_ion = pd.read_csv('data/' + fn_csv + '_ion_drifts.txt', sep=' ')
    if stray_field_script:
        data_stray = pd.read_csv('data/' + fn_csv + '_stray_field_drifts.txt', sep=' ')
    data_atoms = pd.read_csv('data/' + fn_csv + '_atom_drifts.txt', sep=' ')

    # Filtering:
    if filtering:
        for filter_column in ion_filters:
            for expression in ion_filters[filter_column]:
                data_ion.query('`' + filter_column + '`' + expression, inplace=True)
                if verbose_output:
                    print('\033[1mFilter \'{0} {1}\' \033[0m\t=> {0} in {2}'.format(filter_column, expression,
                                                                                    data_ion[filter_column].unique()))

        if stray_field_script:
            for filter_column in stray_filters:
                for expression in stray_filters[filter_column]:
                    data_stray.query('`' + filter_column + '`' + expression, inplace=True)
                    if verbose_output:
                        print('\033[1mFilter \'{0} {1}\' \033[0m\t=> {0} in {2}'.format(filter_column, expression,
                                                                                        data_stray[filter_column].unique()))

        for filter_column in atom_filters:
            for expression in atom_filters[filter_column]:
                data_atoms.query('`' + filter_column + '`' + expression, inplace=True)
                if verbose_output:
                    print('\033[1mFilter \'{0} {1}\' \033[0m\t=> {0} in {2}'.format(filter_column, expression,
                                                                                    data_atoms[filter_column].unique()))

    # Offsetting:
    if offsetting:
        data_ion['radba_yrowsumcut.radba_gaussy.pos'] -= np.average(data_ion['radba_yrowsumcut.radba_gaussy.pos'])
        data_ion['radba_xcolsumcut.radba_gaussx_skewed.pos'] -= np.average(
            data_ion['radba_xcolsumcut.radba_gaussx_skewed.pos'])
        data_atoms['abs_h_cutcolintcut.abs_h_gaussx.pos'] -= np.average(
            data_atoms['abs_h_cutcolintcut.abs_h_gaussx.pos'])

    li_temperatures = data_atoms['eva_ramp_target_p'].unique()

    if stray_field_script:
        drift_data = {
            'Ion position shift [x]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.axcolsumcut.axgaussx.position_shift'],
                                        'unit': 'µm', 'multiplier': 1e6},
            'Ion position shift [y]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.ax_yrowsumcut.axgaussy.position_shift'],
                                        'unit': 'µm', 'multiplier': 1e6},
            'Ion position shift [ax]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.radba_xcolsumcut.radba_gaussx_skewed.position_shift'],
                                        'unit': 'µm', 'multiplier': 1e6},
            'E field [x]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.axcolsumcut.axgaussx.stray_field'],
                                        'unit': 'mV/m', 'multiplier': 1e3},
            'E field [y]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.ax_yrowsumcut.axgaussy.stray_field'],
                                        'unit': 'mV/m', 'multiplier': 1e3},
            'E field [ax]:': {'t': data_stray['_starttime'], 'data': data_stray['stray_field_calc.radba_xcolsumcut.radba_gaussx_skewed.stray_field'],
                                        'unit': 'mV/m', 'multiplier': 1e3},
            'xODT position: ax': {'t': data_atoms['_starttime'], 'data': data_atoms['abs_h_cutcolintcut.abs_h_gaussx.pos'],
                                  'unit': 'µm', 'multiplier': 1e6}
        }
    else:
        drift_data = {
            'Ion position: y': {'t': data_ion['_starttime'], 'data': data_ion['radba_yrowsumcut.radba_gaussy.pos'],
                                'unit': 'µm', 'multiplier': 1e6},
            'Ion position: ax': {'t': data_ion['_starttime'], 'data': data_ion['radba_xcolsumcut.radba_gaussx_skewed.pos'],
                                 'unit': 'µm', 'multiplier': 1e6},
            'xODT position: ax': {'t': data_atoms['_starttime'], 'data': data_atoms['abs_h_cutcolintcut.abs_h_gaussx.pos'],
                                  'unit': 'µm', 'multiplier': 1e6}
        }

    for T_Li in li_temperatures:
        drift_data.update({'Atom number: {0} µK'.format(np.round(conversion_peva_t_li * T_Li * 100, 1)):
                               {'t': data_atoms[data_atoms['eva_ramp_target_p'] == T_Li]['_starttime'],
                                'data': data_atoms[data_atoms['eva_ramp_target_p'] == T_Li][
                                    'abs_h_cutrowintcut.abs_h_gaussy.A0'], 'unit': 'k', 'multiplier': 1e-3}})

    if influx_columns is None:
        influxPlots = 2
        if not download_influx_data:
            if raw_data_vac is None:
                influxPlots -= 1
            if raw_data_temp is None:
                influxPlots -= 1
    else:
        influxPlots = len(influx_columns)

    fig, axe = plt.subplots(nrows=len(drift_data) + 1 + influxPlots - len(li_temperatures) - (4 if stray_field_script else 0), sharex=True,
                            figsize=(8 * 1.618, 16))

    sub_plot = 0
    num_stray_field = 0
    num_stray_shift = 0
    num_atom_numbers = 0
    for enum, elt in enumerate(drift_data):
        # xx_time_iso = [datetime.datetime.utcfromtimestamp(t).replace(tzinfo=datetime.timezone.utc).astimezone(
        #     pytz.timezone('Europe/Berlin')).isoformat() for t in drift_data[elt]['t']]
        # xx_time = [datetime.datetime.strptime(t, '%Y-%m-%dT%H:%M:%S.%f%z') for t in xx_time_iso]
        xx_time = unix_to_dateTime(drift_data[elt]['t'])

        if 'position' in elt and 'shift' not in elt:
            axe[sub_plot].set_ylabel(r'$\Delta x$ [{0}]'.format(drift_data[elt]['unit']))
            axe[sub_plot].set_title(elt)
            xx = xx_time
            yy = drift_data[elt]['multiplier'] * drift_data[elt]['data']
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].scatter(xx, yy)
            sub_plot += 1
        elif 'Atom number' in elt:
            axe[sub_plot].set_ylabel('N [{0}]'.format(drift_data[elt]['unit']))
            axe[sub_plot].set_title('Atom number')
            xx = xx_time
            yy = drift_data[elt]['multiplier'] * drift_data[elt]['data']
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].scatter(xx, yy, label=elt.split(':')[-1])
            axe[sub_plot].legend()
            num_atom_numbers += 1
            if num_atom_numbers == len(li_temperatures):
                sub_plot += 1
        elif 'Ion position shift' in elt:
            axe[sub_plot].set_ylabel(r'$\Delta x$ [{0}]'.format(drift_data[elt]['unit']))
            axe[sub_plot].set_title('Ion position shifts')
            xx = xx_time
            yy = drift_data[elt]['multiplier'] * drift_data[elt]['data']
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].plot(xx, yy, label=elt.split('[')[-1].split(']')[0])
            axe[sub_plot].legend()
            num_stray_shift += 1
            if num_stray_shift == 3:
                sub_plot += 1
        elif 'E field' in elt:
            axe[sub_plot].set_ylabel(r'$\vec E$ [{0}]'.format(drift_data[elt]['unit']))
            axe[sub_plot].set_title('Stray fields')
            xx = xx_time
            yy = drift_data[elt]['multiplier'] * drift_data[elt]['data']
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].plot(xx, yy, label=elt.split('[')[-1].split(']')[0])
            axe[sub_plot].legend()
            num_stray_field += 1
            if num_stray_field == 3:
                sub_plot += 1
        if enum == 0:
            time_min = min(xx_time)
            time_max = max(xx_time)
        time_min = min(time_min, min(xx_time))
        time_max = max(time_max, max(xx_time))

    fig.suptitle('{0} System drifts'.format(date.replace('_', '/')))
    axe[-1].set_xlabel('time')
    axe[-1].xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    #axe[-1].set_xticklabels(axe[-1].get_xticklabels(), rotation=45, ha='center')
    axe[-1].tick_params(axis='x', labelrotation = 45)

    ## InfluxDB:
    if influx_columns is None:
        print('Deprecation warning: Specify influx_columns argument! Ignoring influx_data...')
        if download_influx_data:
            raw_data_temp = query_data('water_tube_temp', 'Temperature',
                                       [time_min, time_max])
            raw_data_vac = query_data('Vaccum_main', 'vacuum_degree',
                                      [time_min, time_max])
        if not (raw_data_temp is None):
            time = raw_data_temp._time
            influx_data = np.array(raw_data_temp.Temperature, dtype=float)
            xx = time
            yy = influx_data
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].plot(xx, yy)
            axe[sub_plot].set_title('Main chamber temperature drift')
            axe[sub_plot].set_ylabel(r'$T~$[°C]')

            sub_plot += 1

        if not (raw_data_vac is None):
            time = raw_data_vac._time
            influx_data = np.array(raw_data_vac.vacuum_degree, dtype=float)
            xx = time
            yy = 1e10 * influx_data
            if sub_plot in binningsubplots:
                xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
            axe[sub_plot].plot(xx, yy)
            axe[sub_plot].set_title('Main chamber ion gauge')
            axe[sub_plot].set_ylabel(r'$p~$[1e-10 mbar]')

            sub_plot += 1
    else:
        if download_influx_data:
            influx_data = [query_data(influx_columns[col]['hardware'], influx_columns[col]['field'], [time_min, time_max]) for col in influx_columns]

        for enum, col in enumerate(influx_columns):
            if not (influx_data[enum] is None):
                xx = influx_data[enum]._time
                yy = np.array(influx_data[enum][influx_columns[col]['field']], dtype=float) * influx_columns[col]['multiplier']
                if sub_plot in binningsubplots:
                    xx, yy = binning_horiz(xx, yy, binningsubplots[sub_plot])
                if influx_columns[col]['scatter']:
                    axe[sub_plot].scatter(xx, yy)
                else:
                    axe[sub_plot].plot(xx, yy)
                axe[sub_plot].set_title(influx_columns[col]['title'])
                axe[sub_plot].set_ylabel(influx_columns[col]['ylabel'])

                sub_plot += 1

    for i in range(len(axe)):
        axe[i].grid(which='major')
        grey = 0.95
        axe[i].grid(which='minor', color=[grey, grey, grey])
        axe[i].xaxis.set_minor_locator(ticker.AutoMinorLocator(10))
        axe[i].tick_params(axis='x', which='minor', bottom=False)

    fig.tight_layout()

    if influx_columns is None:
        return fig, axe, raw_data_vac, raw_data_temp
    else:
        return fig, axe, influx_data

def binning_horiz(xx, yy, binning):
    """把 x/y 数据按均值分箱（binning 个点一组），时间轴自动换算回原格式。

    Args:
        xx: x 数据（可含 datetime）。
        yy: y 数据。
        binning: 每箱点数。

    Returns:
        (bin_xx, bin_yy): 分箱后的坐标。
    """
    if type(xx[0]) == datetime.datetime:
        xx_float = dateTime_to_unix(xx)
    elif type(xx[0]) == pandas.Timestamp:
        xx_float = dateTime_to_unix([x.to_pydatetime() for x in xx])
    else:
        xx_float = xx
    bin_yy, bin_xx_edges, bin_numbers = binned_statistic(xx_float, yy, statistic='mean', bins=np.ceil(len(xx) / binning))
    bin_xx_float = (bin_xx_edges[1:] + bin_xx_edges[:-1]) / 2.
    if type(xx[0]) == datetime.datetime or type(xx[0]) == pandas.Timestamp:
        bin_xx = unix_to_dateTime(bin_xx_float)
    else:
        bin_xx = bin_xx_float
    return bin_xx, bin_yy

def plot_preview(B, P_surv, err_lower, err_upper, date, listate, tint, figaxe=None, preliminary=True, label=None, **kwargs):
    """快速绘制计数实验结果的预览散点图（存活概率 vs 磁场）。

    Args:
        B/P_surv/err_lower/err_upper: 磁场、存活概率及误差。
        date/listate/tint: 元数据（用于自动标签）。
        figaxe: (fig, axe)；None 则新建。
        preliminary (bool): True 时叠加大号 PRELIMINARY 水印。
        label (str): 自定义图例；None 自动生成。
        **kwargs: 传给 errorbar 的额外参数。

    Returns:
        (fig, axe)。
    """
    '''
    A small helper to plot a quick preview of counting experiment results.
    
    :figaxe: tuple
        (fig, axe) to which the given data should be added
        
    :preliminary: bool
        If True, add big PRELIMINARY disclaimers to the plot in order to warn Tobias.
        
    :label: string
        Use this label for the scatter plot instead of automatically generated label. Useful in case of
        combined data plots.
    '''
    if 'linestyle' not in kwargs.keys():
        kwargs['linestyle'] = 'none'
    if figaxe is None:
        fig, axe = plt.subplots()
    else:
        fig, axe = figaxe
    
    if label is None:
        label = r'$t_{int} = ' + '{:.0f}'.format(tint*1e3) + r'\,$ms' + r'\quad {}'.format(date.replace('_', '/'))
    
    axe.errorbar(B, P_surv, yerr=np.array([err_lower, err_upper]), marker='.', elinewidth=.6, capthick=.6,label=label, **kwargs)
    axe.set_xlabel('B [G]')
    axe.set_ylabel(r'P$_{\rm surv}$')
    axe.set_ylim(bottom=-0.1, top=1.1)
    axe.legend()
    
    if preliminary:
        for x in np.linspace(0.2, .8, 4):
            axe.text(x, 1-x, 'PRELIMINARY', transform=axe.transAxes,
            fontsize=60, color='gray', alpha=0.1,
            ha='center', va='center', rotation='30')
            
    fig.tight_layout()
    
    return fig, axe

def quick_previewer(date, listate, tint, dset, reference=[], show_reference=True):
    """按日期/自旋态/积分时间/数据集快速预览：画主图并把参考点画为横线。

    Args:
        date/listate/tint/dset: 元数据（构造文件名）。
        reference (list): 视为参考测量的磁场值。
        show_reference (bool): 是否显示参考线。

    Returns:
        (B, P_surv, err_lower, err_upper, fig, axe)。
    """
    '''
    You want it quicker? Eat this.
    
    :reference: list
        List of x values that should be considered as reference measurement. They can be plotted
        as horizontal lines instead of datapoints with errorbars
        
    :show_reference: bool
        If True references are polotted, else they will just be filtered out.
        
    :returns:
        Plotted data and fig, axe of the plot.
    '''
    fn = metadata_2_fn(date, listate, tint,dset)
    
    B, P_surv, err_lower, err_upper = load_data(fn)
    
    # filter out reference measurement for main plot
    select_dat = np.ones_like(B) == 1
    for ref in reference:
        select_dat = np.logical_and(select_dat, B!=ref)
    
    # main scatter plot
    fig, axe = plot_preview(B[select_dat], P_surv[select_dat], err_lower[select_dat], err_upper[select_dat], date, listate, tint)
    
    # plot references as horizontal lines
    if len(reference) > 0 and show_reference:
        xlim = axe.get_xlim()
        for ref in reference:
            # get value and error for reference
            Pref = P_surv[B==ref]
            err_u = err_upper[B==ref]
            err_l = err_lower[B==ref]
            
            # plot reference value as horizontal line
            color = axe._get_lines.get_next_color()
            axe.hlines(Pref, xlim[0], xlim[1], label='Reference {}G'.format(ref), color=color, linewidth=.5, alpha=.4)
            
            # shade uncertainty of reference measurement
            axe.fill_between(x=xlim, y1=Pref-err_l, y2=Pref+err_u, color=color, alpha=0.1)
        
        axe.set_xlim(xlim)
        axe.legend()
        
    return B, P_surv, err_lower, err_upper, fig, axe

def quick_previewer_combined(fn_list, mode='distinguish', label='', reference=[], show_reference=True, output_data=False, meta=True, verbose_output=False, **kwargs):
    """合并/分别绘制多个数据集的快速预览。

    Args:
        fn_list (list[str]): 数据集文件名列表。
        mode (str): 'combine' 合并计数，'distinguish' 各数据集单独配色。
        label (str): 合并模式下的图例标签。
        reference/show_reference: 参考测量处理。
        output_data (bool): 是否同时返回过滤后的数据。
        meta (bool): 是否从文件名解析元数据。
        verbose_output (bool): 是否打印文件列表。
        **kwargs: 传给绘图函数的参数。

    Returns:
        (fig, axe)[, 数据...]。
    """
    '''
    :fn_list: list of strings
        strings are filenames of datasets
        
    :mode: string
        Can be `combine` to combine counts of datasets or `distinguish` to plot each dataset in an individual color.
        
    :label: string
        Label that is used in case of the combined data plot.
    '''
    # print a list of filenames in the selection
    if verbose_output:
        print('Showing data for:')
        for fn in sorted(fn_list):
            print('  ' + fn)

    if mode == 'distinguish':
        figaxe = None
        for fn in sorted(fn_list):
            # recover metadata from filename
            if meta:
                date, listate, tint, dset = fn_2_metadata(fn)
            else:
                date, listate, tint, dset = ['', 0, 0, 0]
            
            # load data from file
            B, P_surv, err_lower, err_upper = load_data(fn)
            
            # create plot
            figaxe = plot_preview(B, P_surv, err_lower, err_upper, date, listate, tint, figaxe=figaxe, preliminary=False, **kwargs)
        
        return figaxe
    
    if mode == 'combine':
        # we use a pandas dataframe because it makes data combination and simplification very easy
        combined_data = pd.DataFrame()
        
        for fn in sorted(fn_list):
            # load data from file
            B, P_surv, err_lower, err_upper, N_tot, N_inel, N_surv, N_det, N_d52, N_loss = load_data(fn, full_output=True)
            
            # append data to dataframe
            combined_data = combined_data.append(pd.DataFrame({"B":B, "N_surv":N_surv, "N_tot":N_tot}), ignore_index=True)
        
        # sum rows if they belong to the same magnetic field
        combined_data = combined_data.groupby("B").sum()
        combined_data = combined_data.reset_index()

        B = combined_data['B'].array
        N_surv = combined_data['N_surv'].array
        N_tot = combined_data['N_tot'].array
        
        # calculate wilson score intervals
        P_surv, err_lower, err_upper = wilson_err(N_tot, N_surv)
        
        # there can be numerical errors that show as small negative error bars, filter them out
        err_lower = np.where(np.abs(err_lower)<1e-15, 0, err_lower)
        err_upper = np.where(np.abs(err_upper)<1e-15, 0, err_upper)

        # filter out reference measurement for main plot
        select_dat = np.ones_like(B) == 1
        for ref in reference:
            select_dat = np.logical_and(select_dat, B!=ref)        
        
        # create summarizing plot
        fig, axe = plot_preview(B[select_dat], P_surv[select_dat], err_lower[select_dat], err_upper[select_dat],
                                date=None, listate=None, tint=None, preliminary=False, label=label, **kwargs)
        
        # plot references as horizontal lines
        xlim = axe.get_xlim()
        if len(reference) > 0 and show_reference:
            for ref in reference:
                # get value and error for reference
                Pref = P_surv[B==ref]
                err_u = err_upper[B==ref]
                err_l = err_lower[B==ref]

                # plot reference value as horizontal line
                color = axe._get_lines.get_next_color()
                axe.hlines(Pref, xlim[0], xlim[1], label='Reference {}G'.format(ref), color=color, linewidth=.5, alpha=.4)

                # shade uncertainty of reference measurement
                axe.fill_between(x=xlim, y1=Pref-err_l, y2=Pref+err_u, color=color, alpha=0.1)
        
        axe.set_xlim(xlim)
        axe.legend()
        if output_data:
            return (fig, axe), B[select_dat], P_surv[select_dat], err_lower[select_dat], err_upper[select_dat]
        return (fig, axe)

def fn_2_metadata(fn):
    """从文件名解析元数据（日期/自旋态/积分时间/数据集号）。

    Args:
        fn (str): 文件名。

    Returns:
        (date, listate, tint, dset)。
    """
    '''
    Helper function to convert filename to metadata.
    '''
    metadata = fn.split('_')
    date = '_'.join(metadata[:3])
    listate = int(metadata[3].replace('spin',''))
    tint = float(metadata[4].replace('ms',''))*1e-3
    dset = int(metadata[5].replace('.txt',''))
    
    return date, listate, tint, dset

def metadata_2_fn(date, listate, tint, dset):
    """由元数据生成标准文件名。

    Args:
        date: 日期串。listate: 自旋态。tint: 积分时间（s）。dset: 数据集号。

    Returns:
        str: 文件名。
    """
    '''
    Helper function to convert metadata to file name.
    '''
    fn = '{}_spin{}_{:.0f}ms_{}.txt'.format(date, listate, tint*1e3,dset)
    return fn

'''Ion lifetime fitting functions'''

def gaussian_decay_fix(t, tau, c):
    """归一化初值的高斯衰减（A0 固定为 1）。

    Args:
        t: 时间。tau: 时间常数。c: 基线。

    Returns:
        高斯衰减值。
    """
    return gaussian_decay(t, 1, tau, c)

def exp_decay_fix(t, tau, c):
    """归一化初值的指数衰减（A0 固定为 1）。

    Args:
        t: 时间。tau: 时间常数。c: 基线。

    Returns:
        指数衰减值。
    """
    return exp_decay(t, tau, c, 1)

def exp_decay_offset(t, tau, c,tau0):
    """带时间偏移的指数衰减（用 t+tau0 作自变量）。

    Args:
        t: 时间。tau: 时间常数。c: 基线。tau0: 时间偏移。

    Returns:
        衰减值。
    """
    return exp_decay_fix(t+tau0, tau, c)

def N_dot_fit_function(t, gamma_sc, gamma_tbl_down, N_up_0=1, N_down_0=0, gamma_tbl_up=0):
    """基于速率方程的拟合函数：假设仅一个自旋布居存在三体损失。

    Args:
        t: 时间。gamma_sc: 自旋交换率（up→down）。
        gamma_tbl_down: down 态三体损失率。
        N_up_0/N_down_0: 初态布居。gamma_tbl_up: up 态三体损失率。

    Returns:
        存活粒子总数随时间的解。
    """
    '''
    A fit function based on rate equations, assuming that only one spin population exhibits three
    body losses.
    :gamma_sc: float
        Spin exchange rate from up to down state.
    :gamma_tbl_down: float
        Three-body-loss rate from down state.
    :N_up_0: float
        Initial population in up state.
    :N_down_0: float
        Initial population in down state.
    '''
    # initial populations
    N_0 = np.array([N_up_0, N_down_0])
    
    # rate equation matric
    N_dot_mat = np.array([[-gamma_sc - gamma_tbl_up, 0],
                         [gamma_sc, -gamma_tbl_down]])
    # time derivative
    def N_dot(N, t):
        return np.matmul(N_dot_mat, N)
    
    # solver
    sol = odeint(N_dot, N_0, t)
    N_survival = sol.sum(axis=1)
    
    return N_survival

def fit_rate_measurement_data(res, fn=exp_decay_offset, p0=None):
    """对每个 B 的速率测量数据做（默认带偏移）指数衰减拟合并打印 tau。

    Args:
        res (dict): load_rate_measurement_data 的结果。
        fn: 拟合函数。p0: 初值。

    Returns:
        不确定度传播后的拟合参数对象。
    """
    bounds_exp = [(0,0), (np.inf, np.inf)]
    bounds_exp_c = [(0,0,-np.inf), (np.inf, np.inf, np.inf)]
    for B in res.keys():
        tww, P_surv, err_lower, err_upper = res[B]
        
        # fit exponential decay to the data and plot it on top
        if p0 is None:
            p0 = [70, 0.4]
        popt, pcov = curve_fit(fn, tww, P_surv, p0=p0, bounds=bounds_exp)
        popt_uc = uc.correlated_values(popt, pcov)
        print('    tau_exp = {} ms'.format(popt_uc[0]))
        print(' ')
    return popt_uc

def plot_rate_measurement_data(res, non_offset=True, non_c=False, no_fix=False, rate_based=False, gaussian=False, p0=None, N0=None, return_result=True, figaxe=None):
    """绘制速率测量数据并对每个 B 做所选模型的拟合叠加。

    Args:
        res (dict): 速率测量数据。
        non_offset/non_c/no_fix/rate_based/gaussian (bool): 选择拟合模式。
        p0/N0: 拟合初值。
        return_result (bool): 是否同时返回拟合参数。
        figaxe: (fig, axe)；None 新建。

    Returns:
        (fig, axe[, fitted_parameters])。
    """
    if figaxe is None:
        fig, axe = plt.subplots()
    else:
        fig, axe = figaxe
    bounds_exp = [(0,0), (np.inf, np.inf)]
    bounds_exp_c = [(0,0,-np.inf), (np.inf, np.inf, np.inf)]
    fitted_parameters = []
    B_fields = []
    
    for B in res.keys():
        B_fields.append(B)
        popts = []
        tww, P_surv, err_lower, err_upper = res[B]
        err_mean = (err_lower + err_upper)/2

        label = 'B = {} G'.format(B)
        print(label)
        errplot = axe.errorbar(tww, P_surv, yerr=(err_lower, err_upper), linestyle='none', label=label, marker='.')
        
        
        # fit exponential decay to the data and plot it on top
        if non_offset:
            p0_1 = [70]
            fit_func = lambda t, tau: exp_decay_fix(t, tau, c=0)
            bounds = [(0), (np.inf)]

            popt, pcov = curve_fit(fit_func, tww, P_surv, p0=p0_1, bounds=bounds)
            popt_uc = uc.correlated_values(popt, pcov)
            popts.append(popt_uc)
            print('    Simple Exponential')
            print('        tau_exp = {} ms'.format(popt_uc[0]))
            print(' ')
            twwpl = np.linspace(0, max(tww), 400)
            axe.plot(twwpl, fit_func(twwpl, *popt), color=errplot[0].get_color(), label='Exponential decay best fit', linestyle='--')
        
        # fit exponential decay with time offset
        if non_c:
            p0_2 = [70, 0, 50]
            bounds_exp_c = [(0,0,-np.inf), (np.inf, 0.001, np.inf)]
            popt, pcov = curve_fit(exp_decay_offset, tww, P_surv, p0=p0_2, bounds=bounds_exp_c)
            popt_uc = uc.correlated_values(popt, pcov)
            popts.append(popt_uc)
            print('    Exponential with time offset')
            print('        tau_exp_offs = {} ms'.format(popt_uc[0]))
            print('        tau0_exp_offs = {} ms'.format(popt_uc[2]))
            print('        c_exp_offs = {}'.format(popt_uc[1]))
            print(' ')

            sel = twwpl>popt[2]
            axe.plot(twwpl[sel], exp_decay_offset(twwpl[sel], *popt), color=errplot[0].get_color(), label='Exp offs decay best fit', linestyle=':')
        
        if no_fix:
            p0_3 = [70,0,1]
            popt, pcov = curve_fit(exp_decay, tww, P_surv, p0=p0_3, bounds=[0,np.inf])
            popt_uc = uc.correlated_values(popt, pcov)
            popts.append(popt_uc)
            print('    tau_no_fix = {} ms'.format(popt_uc[0]))
            print('    A0_no_fix = {}'.format(popt_uc[2]))
            print('    c_no_fix = {}'.format(popt_uc[1]))
            print(' ')

            sel = twwpl>popt[2]
            axe.plot(twwpl[sel], exp_decay(twwpl[sel], *popt), color=errplot[0].get_color(), label='Exp no A fix best fit', linestyle='--')
            
        # fit a rate based function
        if rate_based:
            bounds_rate_based = [(0,0), (np.inf, np.inf)]
            
            # for the moment keep initial populations fixed
            def fit_func(t, gamma_sc, gamma_tbl_down):
                return N_dot_fit_function(t, gamma_sc, gamma_tbl_down, N0[0], N0[1])
            
            popt, pcov = curve_fit(fit_func, tww, P_surv, p0=p0, bounds=bounds_rate_based, sigma=err_mean, absolute_sigma=True)
            popt_uc = uc.correlated_values(popt, pcov)
            popts.append(popt_uc)
            print('    Rate equation')
            print('        tau_sc = {:.3e} ms'.format(1/popt_uc[0]))
            print('        tau_tbl = {:.3e}'.format(1/popt_uc[1]))
            print(' ')
            axe.plot(twwpl, fit_func(twwpl, *popt), color=errplot[0].get_color(), label='Rate equation best fit', linestyle='-')
            
        if gaussian:
            if p0 is None:
                p0 = [1, 70, 0]
            popt, pcov = curve_fit(gaussian_decay, tww, P_surv, p0=p0, sigma=err_mean, absolute_sigma=True)
            popt_uc = uc.correlated_values(popt, pcov)
            popts.append(popt_uc)
            print('    Gaussian decay')
            print('        A0 = {:.3e} '.format(popt_uc[0]))
            print('        tau = {:.3e} ms'.format(popt_uc[1]))
            print('        c = {:.3e}'.format(popt_uc[2]))
            print(' ')
            axe.plot(twwpl, gaussian_decay(twwpl, *popt), color=errplot[0].get_color(), label='Gaussian decay best fit', linestyle='-.')

            
        fitted_parameters.append(popts)
        
    axe.set_xlabel('t [ms]')
    axe.set_ylabel(r'P$_{\rm surv}$')
    axe.legend()
    
    if return_result:
        return fig, axe, fitted_parameters
    
    return fig, axe


def plot_power_measurement_data(res, non_offset=True, non_c=False, no_fix=False):
    """绘制功率测量数据（存活概率 vs 功率）。

    Args:
        res (dict): 功率测量数据。其余参数保留（预留）。

    Returns:
        (fig, axe)。
    """
    fig, axe = plt.subplots()
    bounds_exp = [(0,0), (np.inf, np.inf)]
    bounds_exp_c = [(0,0,-np.inf), (np.inf, np.inf, np.inf)]
    for B in res.keys():
        tww, P_surv, err_lower, err_upper = res[B]

        label = 'B = {} G'.format(B)
        print(label)
        errplot = axe.errorbar(tww, P_surv, yerr=(err_lower, err_upper), linestyle='none', label=label, marker='.')


    axe.set_xlabel('P [W]')
    axe.set_ylabel(r'P$_{\rm surv}$')
    axe.legend()
    
    return fig, axe

def n1(x,a,c):
    """线性函数 a·x（供 N 依赖拟合用）。

    Args:
        x: 自变量。a: 斜率。c: 截距（未用）。

    Returns:
        a·x。
    """
    return (a*x)

def n2(x,a,c):
    """二次函数 a·x²（供 N 依赖拟合用）。

    Args:
        x: 自变量。a: 系数。c: 截距（未用）。

    Returns:
        a·x²。
    """
    return (a*x**2)

def n3(x,a,c):
    """三次函数 a·x³（供 N 依赖拟合用）。

    Args:
        x: 自变量。a: 系数。c: 截距（未用）。

    Returns:
        a·x³。
    """
    return (a*x**3)

def plot_N_dependence(Natlist, filelist, fn):
    """绘制寿命时间常数对原子数的依赖（对每组文件拟合衰减）。

    Args:
        Natlist: 原子数列表。
        filelist: 对应数据文件列表。
        fn: 拟合函数。
    """
    tau_list = []
    for file in filelist:
        res = load_rate_measurement_data_combined(file)
        popt_uc = fit_rate_measurement_data(res)
        tau_list.append(popt_uc[0])
    print(tau_list)
    
def gaussian_n(x, mu=0, sigma=1):
    """高斯密度轮廓（未归一）：exp(-(x-mu)²/2σ²)。

    Args:
        x: 自变量。mu: 中心。sigma: 宽度。

    Returns:
        高斯轮廓值。
    """
    e = np.exp(-(x-mu)**2/2/sigma**2)
    return e

def three_body_loss(n, t):
    """三体损失模型：存活率 exp(-n²·t)，t 以寿命 τ 为单位。

    Args:
        n: 密度。t: 时间（以 τ 为单位）。

    Returns:
        存活率。
    """
    return np.exp(-n**2*t)

def three_body_loss_spatial_profile(x, t, mu=0, sigma=1):
    """扫描穿过原子云时的离子存活概率空间轮廓（损失率 ∝ 密度²）。

    Args:
        x: 位置。t: 时间。mu/sigma: 原子云高斯中心与宽度。

    Returns:
        空间存活轮廓。
    """
    n = gaussian_n(x, mu, sigma)
    P_surv = three_body_loss(n, t)
    return P_surv

def convert_dates(date, selection_time, verbose_output=False):
    """由日期与时间窗计算 dateTime 数组（同时返回 Unix 格式）。

    selection_time[1] < selection_time[0] 时会给结束时间加一天。

    Args:
        date: 日期。selection_time: 起止时间（长度 1-2）。
        verbose_output (bool): 是否打印详细信息。

    Returns:
        (dateTime 数组, Unix 数组)。
    """
    """
    Calculates the dateTime array out of date and time window and returns dateTime and Unix formats.

    If selection_time[1] < selection_time[0], 1 day will be added onto selection_time[1].

    Example 1:
        date = '2023_06_28'
        post_selection = ['14:00', '22:00']
        returns: [datetime.datetime(2023, 6, 28, 14, 0, tzinfo=<DstTzInfo 'Europe/Berlin' CEST+2:00:00 DST>),
                  datetime.datetime(2023, 6, 28, 22, 0, tzinfo=<DstTzInfo 'Europe/Berlin' CEST+2:00:00 DST>)],
                 [1687953600.0, 1687982400.0]

    Example 2:
        date = '2023_06_28'
        post_selection = ['14:00', '10:00']
        returns: [datetime.datetime(2023, 6, 28, 14, 0, tzinfo=<DstTzInfo 'Europe/Berlin' CEST+2:00:00 DST>),
                  datetime.datetime(2023, 6, 29, 10, 0, tzinfo=<DstTzInfo 'Europe/Berlin' CEST+2:00:00 DST>)],
                 [1687953600.0, 1688025600.0]

    date: [str], date as ['%Y_%m_%d']
    selection_time: [str], start and end time as ['%H:%M', '%H:%M']
    verbose_output: bool, whether to print the return values
    returns: [DateTime], [float (unix format)]
    """

    print('Deprecation warning: Use dateTime_to_unix() instead of convert_dates().')

    if len(selection_time) != 2:
        print('convert_dates() got invalid selection_time window:', selection_time)
        return [], []

    selection_time_datetime = [
        pytz.timezone('Europe/Berlin').localize(datetime.datetime.strptime(date + '_' + time, '%Y_%m_%d_%H:%M')) for
        time in selection_time]
    if np.diff(selection_time_datetime)[0].total_seconds() < 0:
        selection_time_datetime[1] += datetime.timedelta(days=1)

    selection_time_unix = [dt.timestamp() for dt in selection_time_datetime]
    if verbose_output:
        print(f'{selection_time_datetime = }')
        print(f'{selection_time_unix = }')

    return selection_time_datetime, selection_time_unix

def dateTime_to_unix(datetimes):
    """把 datetime（本地时区）换算为 unix 秒（UTC）。

    Args:
        datetimes: datetime 或序列。

    Returns:
        list[float]: unix 时间戳。
    """
    # datetime.datetime is not a numpy scalar: np.isscalar(datetime) is False,
    # so a single datetime used to be treated as iterable and raised
    # TypeError: 'datetime.datetime' object is not iterable (BaLi
    # 02_compensation_helper.py). Wrap single values explicitly; list/tuple/
    # ndarray input keeps the list return contract.
    if isinstance(datetimes, datetime.datetime):
        datetimes = [datetimes]
    return [date.replace(tzinfo=tzlocal()).astimezone(tz=datetime.timezone.utc).timestamp() for date in datetimes]

def unix_to_dateTime(unix):
    """把 unix 秒换算为 Europe/Berlin 时区的 datetime 列表。

    Args:
        unix: unix 时间戳或序列。

    Returns:
        list[datetime]: Berlin 时区时间。
    """
    if np.isscalar(unix):
        unix = [unix]
    xx_time_iso = [datetime.datetime.utcfromtimestamp(t).replace(tzinfo=datetime.timezone.utc).astimezone(
        pytz.timezone('Europe/Berlin')).isoformat() for t in unix]
    xx_time = [datetime.datetime.strptime(t, '%Y-%m-%dT%H:%M:%S.%f%z') for t in xx_time_iso]
    return xx_time
