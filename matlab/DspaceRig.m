classdef DspaceRig < handle
%DSPACERIG  MATLAB -> ControlDesk(COM) -> MicroLabBox(DS1202) 的最小自动化封装。
%
%   在 auto_sim 类脚本里，本类替代 "load_system / set_param / sim / Scope 变量" 这一层：
%     load_system + localResolveBlocks  -> rig.openExperiment + rig.variable(path)
%     set_param(blk,'Value',v)          -> rig.setParam('Model Root/<块名>/Value', v)
%     sim(model)                        -> pause(稳态时间) + rig.record(T, 文件名)
%     Scope -> 工作区变量               -> DspaceRig.loadRecording / DspaceRig.getSignal
%
%   典型用法：
%     rig = DspaceRig();                                  % 连接（或启动）ControlDesk
%     rig.openExperiment('Project_PACC', 'Experiment_PACC');
%     rig.loadApplication('F:\ZYS_HYBASE\pacc_drive.sdf');   % 可选：重新下载 .rta
%     rig.goOnline();                                     % = ControlDesk "Go Online"
%     rig.setParam('Model Root/phi2_deg/Value', 120);
%     rig.configureMeasurement({'Model Root/PHV/In1', 'Model Root/idc1/In1'});
%     matFile = rig.record(0.5, 'F:\runs\case001');       % 录 0.5 s -> case001.mf4 + case001.mat
%     rec = DspaceRig.loadRecording(matFile);
%     d   = DspaceRig.getSignal(rec, 'Model Root/idc1/In1');   % d.t, d.y, d.available
%     rig.goOffline();
%
%   依赖：Windows + ControlDesk 2023-A（COM ProgID "ControlDeskNG.Application"）+ 64 位 MATLAB。
%   接口出处：ControlDesk Automation 手册（Direct Variable Access p.128, Measurement/Recording
%   p.144, RegisteredDS1202Platform p.1789, ControllableRealTimeApplication p.622）。
%   注意：该类尚未在 PACC 台架上实测，首次使用请先跑 dspace_selftest.m（只读 + 短录制）。

    properties
        app                                   % ControlDeskNG.Application COM 句柄
        platform                              % ActiveExperiment.Platforms 中的 DS1202 平台
        platformName char = 'Platform'        % 实验里平台名（导出的 MAT 里 Device 字段同名）
        raster char = 'HostService'           % PACC_Drive.trc: sampling_period[0] alias, 0.1 ms
        sigconvExe char = 'C:\Program Files\dSPACE ControlDesk 2023-A\Main\bin\DSSigConv.exe'
        pollPeriod double = 0.2               % 状态轮询周期 (s)
        verbose logical = true
    end

    properties (Access = private)
        vars                                  % ActiveVariableDescription.Variables
        workingDataSetActive logical = false
    end

    %% ------------------------------------------------------------ 连接 / 实验
    methods
        function obj = DspaceRig()
            obj.app = actxserver('ControlDeskNG.Application');
            obj.log('ControlDesk COM 已连接');
        end

        function openExperiment(obj, projectName, experimentName)
        %OPENEXPERIMENT 打开工程并激活实验，然后缓存平台与变量集合。
            a = obj.app;
            needOpen = true;
            try
                needOpen = ~strcmp(char(a.ActiveProject.Name), projectName);
            catch
            end
            if needOpen
                roots = obj.items(a.ProjectRoots);
                found = false;
                for k = 1:numel(roots)
                    if invoke(roots{k}.Projects, 'Contains', projectName)
                        invoke(roots{k}, 'Activate');
                        invoke(roots{k}.Projects.Item(projectName), 'Open');
                        found = true;
                        break;
                    end
                end
                if ~found
                    error('DspaceRig:project', ...
                        '在 ControlDesk 的所有 Project Root 中找不到工程 "%s"。', projectName);
                end
            end
            needActivate = true;
            try
                needActivate = ~strcmp(char(a.ActiveExperiment.Name), experimentName);
            catch
            end
            if needActivate
                invoke(a.ActiveProject.Experiments.Item(experimentName), 'Activate');
            end
            obj.platform = obj.firstItem(a.ActiveExperiment.Platforms);
            obj.platformName = char(obj.platform.Name);
            obj.vars = obj.platform.ActiveVariableDescription.Variables;
            obj.workingDataSetActive = false;
            obj.log('工程 %s / 实验 %s 已激活，平台 "%s"', projectName, experimentName, obj.platformName);
        end

        function release(obj)
            try
                delete(obj.app);
            catch
            end
            obj.app = [];
        end
    end

    %% ------------------------------------------------------------ 实时应用 (.rta) 加载 / 启停
    methods
        function loadApplication(obj, sdfPath, startAfterLoad)
        %LOADAPPLICATION 把 .sdf 对应的实时应用下载到 MicroLabBox（相当于 ControlDesk 里
        %   Platform 右键 -> Load Real-Time Application）。等价于 rti_build2(...,'Command','L')。
            if nargin < 3
                startAfterLoad = true;
            end
            if ~isfile(sdfPath)
                error('DspaceRig:sdf', '找不到 %s', sdfPath);
            end
            reg = obj.registeredPlatform();
            obj.log('下载实时应用 %s ...', sdfPath);
            try
                % (path, UnloadConflictingApplication, StartAfterLoading,
                %  ReducedCompatibilityBehavior: 0 Abort / 1 FixReduced / 2 IgnoreReduced)
                invoke(reg, 'LoadRealtimeApplicationControlled', sdfPath, true, startAfterLoad, int32(1));
            catch ME1
                obj.log('LoadRealtimeApplicationControlled 失败 (%s)，改用 LoadRealtimeApplication', ME1.message);
                invoke(reg, 'LoadRealtimeApplication', sdfPath);
            end
            if startAfterLoad
                obj.waitFor(@() obj.applicationState() == 1, 60, '实时应用进入 Running');
            end
        end

        function st = applicationState(obj)
        %APPLICATIONSTATE 0 Undefined,1 Running,2 Stopped,3 Terminated,4 RunningFromFlash,... NaN 未知
            st = NaN;
            try
                rta = obj.firstItem(obj.registeredPlatform().RealTimeApplications);
                st = double(rta.State);
            catch
            end
        end

        function startApplication(obj)
            rta = obj.firstItem(obj.registeredPlatform().RealTimeApplications);
            invoke(rta, 'Start');
            obj.waitFor(@() obj.applicationState() == 1, 30, '实时应用 Start');
        end

        function stopApplication(obj)
            rta = obj.firstItem(obj.registeredPlatform().RealTimeApplications);
            invoke(rta, 'Stop');
            obj.waitFor(@() any(obj.applicationState() == [2 7]), 30, '实时应用 Stop');
        end
    end

    %% ------------------------------------------------------------ 在线标定（Go Online）
    methods
        function goOnline(obj)
        %GOONLINE 等价于 ControlDesk 工具栏 "Go Online"（Start Online Calibration）。
            obj.requirePlatform();
            invoke(obj.app.CalibrationManagement, 'StartOnlineCalibration');
            obj.waitFor(@() double(obj.platform.CalibrationState) == 1, 30, 'Online Calibration');
            % 写参数前必须激活 Working Data Set（手册 p.131）
            invoke(obj.platform.ActiveVariableDescription.DataSets.WorkingDataSet, 'Activate');
            obj.workingDataSetActive = true;
            obj.log('已上线 (online calibration started)');
        end

        function goOffline(obj)
            try
                invoke(obj.app.MeasurementDataManagement, 'Stop');
            catch
            end
            invoke(obj.app.CalibrationManagement, 'StopOnlineCalibration');
            obj.workingDataSetActive = false;
            obj.log('已离线');
        end

        function tf = isOnline(obj)
            tf = false;
            try
                tf = double(obj.platform.CalibrationState) == 1;
            catch
            end
        end
    end

    %% ------------------------------------------------------------ 参数读写（替代 set_param）
    methods
        function var = variable(obj, path)
        %VARIABLE 按路径取变量对象。path 形如 'Model Root/phi2_deg/Value'。
            obj.requirePlatform();
            candidates = {path, ...
                sprintf('%s()://%s', obj.platformName, path), ...
                sprintf('%s(%s)://%s', obj.platformName, obj.raster, path)};
            for k = 1:numel(candidates)
                try
                    var = obj.vars.Item(candidates{k});
                    if ~isempty(var)
                        return;
                    end
                catch
                end
            end
            error('DspaceRig:variable', ...
                ['变量 "%s" 不在当前变量描述中。可用 rig.listVariables(''%s'') 查找真实名称，' ...
                 '或在 ControlDesk Variables 面板右键 Copy 得到路径。'], path, regexptranslate('escape', path));
        end

        function v = getParam(obj, path)
            v = obj.variable(path).ValueConverted;
        end

        function setParam(obj, path, value, relTol)
        %SETPARAM 写标量参数并回读校验（默认相对误差 1e-6）。
            if nargin < 4
                relTol = 1e-6;
            end
            if ~obj.workingDataSetActive
                invoke(obj.platform.ActiveVariableDescription.DataSets.WorkingDataSet, 'Activate');
                obj.workingDataSetActive = true;
            end
            var = obj.variable(path);
            var.ValueConverted = double(value);
            back = double(var.ValueConverted);
            if abs(back - value) > relTol * max(1, abs(value))
                error('DspaceRig:writeback', '写入 %s = %.15g 后回读为 %.15g', path, value, back);
            end
            obj.log('  %s = %.6g', path, value);
        end

        function setParams(obj, pairs)
        %SETPARAMS pairs 为 N x 2 cell：{path, value; ...}
            for k = 1:size(pairs, 1)
                obj.setParam(pairs{k, 1}, pairs{k, 2});
            end
        end

        function tbl = listVariables(obj, pattern)
        %LISTVARIABLES 列出（可选 regexp 过滤）变量描述中的全部变量，便于建立 仿真块名 -> 硬件路径 映射。
            obj.requirePlatform();
            if nargin < 2
                pattern = '';
            end
            all = obj.items(obj.vars);
            n = numel(all);
            name = strings(n, 1);
            type = NaN(n, 1);
            writable = false(n, 1);
            readable = false(n, 1);
            for k = 1:n
                v = all{k};
                try
                    name(k) = string(v.Identifier.UniqueName);
                catch
                    try
                        name(k) = string(v.Identifier.Path);
                    catch
                        name(k) = "";
                    end
                end
                try
                    type(k) = double(v.Type);
                    writable(k) = logical(v.IsWritable);
                    readable(k) = logical(v.IsReadable);
                catch
                end
            end
            tbl = table(name, type, readable, writable);
            if ~isempty(pattern)
                tbl = tbl(~cellfun(@isempty, regexpi(cellstr(tbl.name), pattern, 'once')), :);
            end
        end
    end

    %% ------------------------------------------------------------ 测量 / 录制（替代 sim + Scope）
    methods
        function configureMeasurement(obj, paths, bufferSizeSec)
        %CONFIGUREMEASUREMENT 用给定信号重建测量信号列表（等价于 ControlDesk Measurement Configuration）。
        %   paths: cell，如 {'Model Root/PHV/In1', 'Model Root/Scope7/In1', ...}
            obj.requirePlatform();
            if nargin < 3
                bufferSizeSec = 10;
            end
            paths = cellstr(paths);
            mdm = obj.app.MeasurementDataManagement;
            sigs = mdm.MeasurementConfiguration.Signals;
            invoke(sigs, 'RemoveAll');
            for k = 1:numel(paths)
                if isempty(paths{k})
                    continue;
                end
                s = invoke(sigs, 'Add', obj.signalDefinition(paths{k}));
                try
                    s.Active = true;
                catch
                end
            end
            try
                mdm.MeasurementConfiguration.Buffer.Size = bufferSizeSec;
            catch
            end
            obj.log('测量信号列表: %d 个信号 (raster %s)', numel(paths), obj.raster);
        end

        function def = signalDefinition(obj, path)
        %SIGNALDEFINITION 'Model Root/X/In1' -> 'Platform(HostService)://Model Root/X/In1'
            if contains(path, '://')
                def = path;
            else
                def = sprintf('%s(%s)://%s', obj.platformName, obj.raster, path);
            end
        end

        function [matFile, mf4File] = record(obj, durationSec, baseName)
        %RECORD 用 Main Recorder 定时录制 durationSec 秒，产出 <baseName>.mf4 与 <baseName>.mat。
        %   MAT 结构（已在工位上用 DSSigConv 验证）：S.<文件名>.X(k).Data 时间轴，
        %   S.<文件名>.Y(i).Name/Path/Raster/XIndex/Data。baseName 只能含字母数字下划线
        %   （它会成为 MAT 里的变量名）。
            [d, n] = fileparts(baseName);
            if isempty(d)
                d = pwd;
            end
            if ~isvarname(n)
                error('DspaceRig:name', '录制文件名 "%s" 必须是合法 MATLAB 变量名。', n);
            end
            if ~exist(d, 'dir')
                mkdir(d);
            end
            mf4File = fullfile(d, [n '.mf4']);
            matFile = fullfile(d, [n '.mat']);
            for f = {mf4File, matFile}
                if isfile(f{1})
                    delete(f{1});
                end
            end

            mdm = obj.app.MeasurementDataManagement;
            rec = mdm.MainRecorder;
            rec.BaseFileName = mf4File;
            rec.AutomaticNamingEnabled = false;
            try
                rec.AddToExperimentEnabled = false;
                rec.OpenInMeasurementDataPoolEnabled = false;
            catch
            end
            exportByRecorder = true;
            try
                rec.ExportFullPath = matFile;   % 录完自动导出 MAT（扩展名决定格式）
            catch
                exportByRecorder = false;
            end
            sc = rec.StopCondition;
            sc.Enabled = true;
            sc.Type = int32(0);                 % StopConditionType.TimeLimit
            sc.TimeLimit = durationSec;

            try
                invoke(mdm, 'Start');           % 测量须处于运行态；已在运行时可能抛异常，忽略
            catch
            end
            invoke(rec, 'Start', false, true);  % (WithTrigger=false, OverwriteExisting=true)
            obj.log('录制 %.2f s -> %s', durationSec, mf4File);
            obj.waitFor(@() double(rec.State) == 0, durationSec + 60, '录制结束');   % 0 = Idling

            try
                files = rec.LastRecordedFiles;
                if iscell(files) && ~isempty(files)
                    mf4File = char(files{1});
                elseif ischar(files) || isstring(files)
                    mf4File = char(string(files(1)));
                end
            catch
            end
            obj.waitFor(@() isfile(mf4File), 30, 'mf4 文件落盘');
            if ~(exportByRecorder && obj.waitFor(@() isfile(matFile), 30, 'MAT 导出', false))
                obj.convertMf4(mf4File, matFile);
            end
        end

        function convertMf4(obj, mf4File, matFile)
        %CONVERTMF4 用 ControlDesk 自带命令行工具 DSSigConv 把 .mf4 转成 .mat（已验证可用）。
            if ~isfile(obj.sigconvExe)
                error('DspaceRig:sigconv', '找不到 %s', obj.sigconvExe);
            end
            cmd = sprintf('"%s" "%s" "%s"', obj.sigconvExe, mf4File, matFile);
            [st, out] = system(cmd);
            if st ~= 0 || ~isfile(matFile)
                error('DspaceRig:sigconv', 'DSSigConv 失败: %s', out);
            end
            obj.log('DSSigConv -> %s', matFile);
        end
    end

    %% ------------------------------------------------------------ 录制数据读取（替代 localFetchSignal）
    methods (Static)
        function rec = loadRecording(matFile)
        %LOADRECORDING 读入 ControlDesk 导出的 MAT，整理为 rec.time{k} 与 rec.signals(i)。
            S = load(matFile);
            fn = fieldnames(S);
            raw = S.(fn{1});
            rec = struct('file', matFile, 'time', {{}}, 'signals', []);
            rec.time = arrayfun(@(x) double(x.Data(:)), raw.X, 'UniformOutput', false);
            n = numel(raw.Y);
            tmpl = struct('key', '', 'name', '', 'path', '', 'raster', '', 'xIndex', 1, 'data', []);
            rec.signals = repmat(tmpl, n, 1);
            for i = 1:n
                y = raw.Y(i);
                p = '';
                if isfield(y, 'Path')
                    p = char(y.Path);
                end
                nm = char(y.Name);
                if isempty(p)
                    key = nm;
                else
                    key = [p '/' nm];
                end
                rec.signals(i).key = key;
                rec.signals(i).name = nm;
                rec.signals(i).path = p;
                if isfield(y, 'Raster')
                    rec.signals(i).raster = char(y.Raster);
                end
                if isfield(y, 'XIndex')
                    rec.signals(i).xIndex = double(y.XIndex);
                end
                rec.signals(i).data = double(y.Data(:));
            end
        end

        function d = getSignal(rec, keys)
        %GETSIGNAL 取一个或多个信号拼成 [t, y(:,1..m)]，输出与 auto_sim 的 localNormalizeSignal 同构：
        %   d.available / d.t (N x 1) / d.y (N x m)。keys 可为 'Model Root/Scope7/In1' 或 cell。
        %   匹配规则：完整 key 相等，或 key 以 '/<keys>' 结尾（可只写 'Scope7/In1'）。
            d = struct('available', false, 't', [], 'y', []);
            if isempty(rec) || isempty(rec.signals)
                return;
            end
            keys = cellstr(keys);
            allKeys = {rec.signals.key};
            y = [];
            t = [];
            for k = 1:numel(keys)
                if isempty(keys{k})
                    return;
                end
                idx = find(strcmpi(allKeys, keys{k}), 1);
                if isempty(idx)
                    idx = find(endsWith(allKeys, ['/' keys{k}], 'IgnoreCase', true), 1);
                end
                if isempty(idx)
                    warning('DspaceRig:signal', '录制文件中没有信号 "%s"', keys{k});
                    return;
                end
                s = rec.signals(idx);
                tk = rec.time{s.xIndex};
                if isempty(t)
                    t = tk;
                elseif numel(tk) ~= numel(t)
                    warning('DspaceRig:signal', '信号 "%s" 与其它列时间轴长度不同', keys{k});
                    return;
                end
                y = [y s.data(:)]; %#ok<AGROW>
            end
            d.t = t(:);
            d.y = y;
            d.available = ~isempty(y);
        end
    end

    %% ------------------------------------------------------------ 内部工具
    methods (Access = private)
        function reg = registeredPlatform(obj)
        %REGISTEREDPLATFORM Platform Manager 中注册的 DS1202（PlatformType.DS1202 = 30）。
            regs = obj.items(obj.app.PlatformManagement.Platforms);
            for k = 1:numel(regs)
                try
                    if double(regs{k}.Type) == 30
                        reg = regs{k};
                        return;
                    end
                catch
                end
            end
            if numel(regs) == 1
                reg = regs{1};
                return;
            end
            error('DspaceRig:noPlatform', 'Platform Manager 中没有已注册的 DS1202/MicroLabBox。');
        end

        function requirePlatform(obj)
            if isempty(obj.platform)
                error('DspaceRig:noExperiment', '请先调用 openExperiment。');
            end
        end

        function ok = waitFor(obj, cond, timeoutSec, what, throwOnTimeout)
            if nargin < 5
                throwOnTimeout = true;
            end
            t0 = tic;
            ok = false;
            while toc(t0) < timeoutSec
                try
                    if cond()
                        ok = true;
                        return;
                    end
                catch
                end
                pause(obj.pollPeriod);
            end
            if throwOnTimeout
                error('DspaceRig:timeout', '等待 "%s" 超时 (%.0f s)', what, timeoutSec);
            end
        end

        function log(obj, fmt, varargin)
            if obj.verbose
                fprintf(['[DspaceRig] ' fmt '\n'], varargin{:});
            end
        end
    end

    methods (Static, Access = private)
        function c = items(coll)
        %ITEMS COM 集合 -> cell（自动探测 Item 的起始下标是 0 还是 1）。
            n = double(coll.Count);
            c = cell(1, n);
            if n == 0
                return;
            end
            base = 0;
            try
                coll.Item(0);
            catch
                base = 1;
            end
            for k = 1:n
                c{k} = coll.Item(k - 1 + base);
            end
        end

        function it = firstItem(coll)
            c = DspaceRig.items(coll);
            if isempty(c)
                error('DspaceRig:empty', '集合为空。');
            end
            it = c{1};
        end
    end
end
