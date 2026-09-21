%% exp9_auto_dspace.m - 固定角度独立确认测试（MicroLabBox / DS1202 硬件版）
% 由 exp9_auto_sim.m 改造而来。分析层（localAnalyzeCase 及其以下）原样保留，
% 只把"模型层"换成 DspaceRig：
%
%   exp9_auto_sim.m                         exp9_auto_dspace.m
%   ------------------------------------    ------------------------------------------------
%   load_system / localResolveBlocks        rig.openExperiment + localResolveVariables
%   localConfigureModel (StopTime...)       cfg.settleTime / cfg.recordTime
%   localConfigureScopeLogging              rig.configureMeasurement(cfg.sig 列表)
%   localApplyCase: set_param(...,'Value')  rig.setParam('Model Root/<块>/Value', v)
%   sim(model,'ReturnWorkspaceOutputs')     pause(cfg.settleTime); rig.record(cfg.recordTime, file)
%   localFetchSignal(simOut, name)          DspaceRig.getSignal(rec, cfg.sig.<name>)
%   负载电阻 / 直流源幅值 set_param          不可软件设定 -> 只记录在 cfg 里，人工保证
%
% 输出：auto_results_exp9_dspace/exp9_run_<timestamp>/exp9_confirm.csv|.mat + 每个角度的 .mf4/.mat 原始录制
%
% !! cfg.var / cfg.sig 中标有 TODO 的路径需要你对照 PACC_Drive.slx 确认（见手册第 6 节）。

clearvars;
clc;
close all;

cfg = localDefaultConfig();
if ~exist(cfg.outRoot, 'dir')
    mkdir(cfg.outRoot);
end
runStamp = datestr(now, 'yyyymmdd_HHMMSS');
runDir = fullfile(cfg.outRoot, ['exp9_run_' runStamp]);
mkdir(runDir);

rig = DspaceRig();
rig.raster = cfg.raster;
rig.openExperiment(cfg.project, cfg.experiment);
if cfg.reloadApplication
    rig.loadApplication(cfg.sdfFile, true);
end
rig.goOnline();
cleanupObj = onCleanup(@() localSafeShutdown(rig, cfg)); %#ok<NASGU>

localResolveVariables(rig, cfg);
rig.configureMeasurement(struct2cell(cfg.sig), cfg.recordTime + 5);
localApplyOperatingPoint(rig, cfg);

results = repmat(localResultTemplate(), 0, 1);
for k = 1:numel(cfg.angles)
    ang = cfg.angles(k);
    fprintf('\n[%d/%d] angle = %.0f deg\n', k, numel(cfg.angles), ang);
    r = localResultTemplate();
    r.angleDeg = ang;
    try
        localApplyCase(rig, cfg, ang);
        pause(cfg.settleTime);                                   % 等待硬件进入稳态（替代仿真的 0~analysisStart 段）
        recBase = fullfile(runDir, sprintf('exp9_phi_%03d', ang));
        matFile = rig.record(cfg.recordTime, recBase);
        rec = DspaceRig.loadRecording(matFile);
        d.IH = DspaceRig.getSignal(rec, cfg.sig.IH);
        d.P1 = DspaceRig.getSignal(rec, cfg.sig.P1);
        d.P2 = DspaceRig.getSignal(rec, cfg.sig.P2);
        d.reconCurve = DspaceRig.getSignal(rec, cfg.sig.reconCurve);
        d.dcCurve = DspaceRig.getSignal(rec, cfg.sig.dcCurve);
        d.valid = DspaceRig.getSignal(rec, cfg.sig.valid);
        d = localShiftTime(d, cfg);                              % 录制从 0 开始，对齐 analysisStart/End 窗口
        r = localAnalyzeCase(d, cfg, ang);
        r.recordFile = string(matFile);
        if r.status == "pending"
            r.status = "ok";
        end
        fprintf('  reconJmin=%.1f  dcJmin=%.1f  fftHighWide=%.4f\n', ...
            r.reconJMinDeg, r.dcJMinDeg, r.fftHighWideRss);
    catch ME
        r.status = "failed";
        r.errorMessage = getReport(ME, 'extended', 'hyperlinks', 'off');
        fprintf(2, '  failed: %s\n%s\n', ME.message, r.errorMessage);
    end
    results(end+1) = r; %#ok<SAGROW,AGROW>
    save(fullfile(runDir, 'results_partial.mat'), 'results', 'cfg');
    clear rec d r;
end

tbl = localBuildTable(results, cfg);
writetable(tbl, fullfile(runDir, 'exp9_confirm.csv'));
save(fullfile(runDir, 'exp9_confirm.mat'), 'results', 'cfg', 'tbl');
fprintf('\nFinished. CSV: %s\n', fullfile(runDir, 'exp9_confirm.csv'));
disp(tbl);

%% 本地函数 ---------------------------------------------------------------

function cfg = localDefaultConfig()
    cfg.projectDir = fileparts(mfilename('fullpath'));
    if isempty(cfg.projectDir)
        cfg.projectDir = pwd;
    end
    cfg.outRoot = fullfile(cfg.projectDir, 'auto_results_exp9_dspace');

    % --- ControlDesk / 硬件 ---
    cfg.project = 'Project_PACC';
    cfg.experiment = 'Experiment_PACC';
    cfg.sdfFile = 'F:\ZYS_HYBASE\pacc_drive.sdf';
    cfg.reloadApplication = false;       % true: 每次运行先重新下载 .rta（相当于 rti_build2 'L'）
    cfg.raster = 'HostService';          % PACC_Drive.trc: 0.1 ms
    cfg.hostTs = 1e-4;
    cfg.disableOnExit = false;           % true: 脚本结束/出错时把 enable 写 0（请按台架安全策略决定）

    % --- 时序（替代 stopTime / analysisStart） ---
    cfg.settleTime = 2.0;                % 改参数后等待稳态 (s)
    cfg.recordTime = 0.5;                % 每个工况录制时长 (s)
    cfg.analysisStart = 0.10;            % 录制文件内的分析窗口（录制从 0 开始计时）
    cfg.analysisEnd = 0.50;
    cfg.curveMeanStart = 0.12;

    % --- 分析参数（与 exp9_auto_sim 相同，但采样率改为硬件 raster）---
    cfg.analysisFs = 1 / cfg.hostTs;     % 10 kHz。fsw/2fsw 纹波指标在此采样率下不可观测，见手册第 7 节
    cfg.fundamentalHz = 50;
    cfg.Tsp = 1e-4;
    cfg.fsw = 1 / cfg.Tsp;
    cfg.angles = [92 112 120 180];

    % --- 运行点（与 exp8 A_M075_xi052 相同）---
    cfg.dcHighVoltage = 240;             % 硬件：由直流源人工设定，脚本只记录
    cfg.dcLowVoltage = 120;              % 硬件：同上
    cfg.xi = 0.52;
    cfg.modulationIndex = 0.75;
    cfg.udRef1V = cfg.modulationIndex * cfg.dcHighVoltage / sqrt(3);
    cfg.udRef2V = cfg.udRef1V;
    cfg.load1ResistanceOhm = 18;         % 硬件：负载箱人工设定，脚本只记录
    cfg.load2ResistanceOhm = 18;
    cfg.acPhaseDeg = 0;

    % --- 可写参数路径（PACC_Drive.trc 中 flags: PARAM 的项）---
    % 已在 TRC 中确认存在：phi2_deg, ud_ref, ud_ref1, ud_ref2, ud_ref3, uq_ref*, enable, Enable1,
    % f1..f6, Constant2/3/5/21, *_offset。仿真模型里的 power_distribute_factor /
    % AC_PhaseShiftAngle 在硬件模型中没有同名块，需要你在 PACC_Drive.slx 里找到对应块后填入。
    cfg.var.phaseShift = 'Model Root/phi2_deg/Value';        % 载波移相角 (deg)   <- 对应 sim 的 phase_shift
    cfg.var.udRef1 = 'Model Root/ud_ref/Value';              % TODO 确认：AC1 d 轴电压参考
    cfg.var.udRef2 = 'Model Root/ud_ref2/Value';             % TODO 确认：AC2 d 轴电压参考
    cfg.var.powerFactor = '';                                % TODO: ξ 对应块（sim: power_distribute_factor）
    cfg.var.acPhase = '';                                    % TODO: AC 相位差 (sim: AC_PhaseShiftAngle, rad)
    cfg.var.enable = 'Model Root/enable/Value';              % PWM 使能（仅 disableOnExit 时使用）

    % --- 测量信号（Scope 块 -> 'Model Root/<Scope名>/In<k>'）---
    % 空字符串表示硬件模型没有该信号，对应指标输出 NaN。
    cfg.sig.IH = 'Model Root/idc1/In1';                      % TODO 确认：高压侧直流电流  (sim Scope78)
    cfg.sig.P1 = 'Model Root/Ic1/In1';                       % TODO 确认：AC1 节点电流    (sim Scope26)
    cfg.sig.P2 = 'Model Root/Ic2/In1';                       % TODO 确认：AC2 节点电流    (sim Scope52)
    cfg.sig.reconCurve = '';                                 % sim Scope83 J 曲线(1x544)，硬件模型无此输出
    cfg.sig.dcCurve = '';                                    % sim Scope89
    cfg.sig.valid = '';                                      % sim Scope85 观测器有效标志
    cfg.sig.phiOpt = 'Model Root/phi_opt/In1';               % 硬件在线寻优输出（额外记录）
    cfg.sig.PHV = 'Model Root/PHV/In1';
    cfg.sig.PLV = 'Model Root/PLV/In1';
end

function localResolveVariables(rig, cfg)
%LOCALRESOLVEVARIABLES 对应 localResolveBlocks：确认每个非空路径都能在变量描述中找到。
    f = fieldnames(cfg.var);
    for i = 1:numel(f)
        p = cfg.var.(f{i});
        if isempty(p)
            fprintf('  [var] %-12s : (未配置，跳过)\n', f{i});
            continue;
        end
        v = rig.getParam(p);
        fprintf('  [var] %-12s : %s = %.6g\n', f{i}, p, v);
    end
end

function localApplyOperatingPoint(rig, cfg)
%LOCALAPPLYOPERATINGPOINT 固定运行点参数只写一次（对应 localApplyCase 中与角度无关的部分）。
    pairs = {};
    if ~isempty(cfg.var.udRef1),      pairs(end+1, :) = {cfg.var.udRef1, cfg.udRef1V}; end
    if ~isempty(cfg.var.udRef2),      pairs(end+1, :) = {cfg.var.udRef2, cfg.udRef2V}; end
    if ~isempty(cfg.var.powerFactor), pairs(end+1, :) = {cfg.var.powerFactor, cfg.xi}; end
    if ~isempty(cfg.var.acPhase),     pairs(end+1, :) = {cfg.var.acPhase, deg2rad(cfg.acPhaseDeg)}; end
    if ~isempty(pairs)
        rig.setParams(pairs);
    end
    fprintf('  (硬件人工设定) VH=%g V, VL=%g V, R1=%g Ω, R2=%g Ω\n', ...
        cfg.dcHighVoltage, cfg.dcLowVoltage, cfg.load1ResistanceOhm, cfg.load2ResistanceOhm);
end

function localApplyCase(rig, cfg, ang)
    rig.setParam(cfg.var.phaseShift, ang);
end

function localSafeShutdown(rig, cfg)
    try
        if cfg.disableOnExit && ~isempty(cfg.var.enable)
            rig.setParam(cfg.var.enable, 0);
        end
    catch
    end
    try
        rig.goOffline();
    catch
    end
end

function d = localShiftTime(d, cfg) %#ok<INUSD>
%LOCALSHIFTTIME 录制文件时间轴从录制起点计（约 0 s），无需平移；保留钩子以便将来对齐触发时刻。
end

%% ---- 以下分析函数与 exp9_auto_sim.m 相同（仅 localAnalyzeCase 增加采样率守卫）----

function r = localAnalyzeCase(d, cfg, ang)
    r = localResultTemplate();
    r.angleDeg = ang;

    tq = (cfg.analysisStart:1/cfg.analysisFs:cfg.analysisEnd).';
    IH = localResample(d.IH, 1, tq);
    P1 = localResample(d.P1, 1, tq);
    P2 = localResample(d.P2, 1, tq);
    if isempty(IH) || isempty(P1) || isempty(P2)
        r.status = "missing_signals";
        return;
    end

    if cfg.analysisFs < 4 * cfg.fsw
        % HostService 10 kHz 采样看不到 fsw/2fsw 载波群；这些指标需要示波器数据（手册第 7 节）
        r.status = "ok_no_ripple";
    else
        Psum = P1 + P2;
        [r.fftHighNarrowRss, r.fftHighWideRss] = localRss12(tq, IH, cfg);
        [r.fftPsumNarrowRss, r.fftPsumWideRss] = localRss12(tq, Psum, cfg);

        [E1hN, E2hN, E1hW, E2hW] = localBandEnergy(tq, IH, cfg);
        [E1pN, E2pN, E1pW, E2pW] = localBandEnergy(tq, Psum, cfg);
        r.g1Narrow = E1hN / max(E1pN, eps);
        r.g2Narrow = E2hN / max(E2pN, eps);
        r.g1g2Narrow = r.g1Narrow / max(r.g2Narrow, eps);
        r.g1Wide = E1hW / max(E1pW, eps);
        r.g2Wide = E2hW / max(E2pW, eps);
        r.g1g2Wide = r.g1Wide / max(r.g2Wide, eps);
    end
    r.IHMeanA = mean(IH, 'omitnan');
    r.IHRmsA = rms(IH - mean(IH, 'omitnan'));

    [r.reconJMinDeg, r.reconJ1MinDeg, r.reconJ2MinDeg] = ...
        localCurveMins(d.reconCurve, cfg);
    [r.dcJMinDeg, r.dcJ1MinDeg, r.dcJ2MinDeg] = ...
        localCurveMins(d.dcCurve, cfg);

    if d.valid.available && size(d.valid.y, 2) >= 2
        idx = d.valid.t >= cfg.analysisStart & d.valid.t <= cfg.analysisEnd;
        idx = idx & isfinite(d.valid.t);
        if any(idx)
            r.observerValidationAvailable = true;
            r.validCount = sum(d.valid.y(idx, 2) > 0.5, 'omitnan');
        end
    end
end

function yq = localResample(d, col, tq)
    yq = [];
    if ~d.available || size(d.y, 2) < col
        return;
    end
    t = d.t(:);
    y = double(d.y(:, col));
    ok = isfinite(t) & isfinite(y);
    t = t(ok);
    y = y(ok);
    [t, ia] = unique(t, 'stable');
    y = y(ia);
    if numel(t) < 2
        return;
    end
    yq = interp1(t, y, tq, 'linear', 'extrap');
end

function [narrow, wide] = localRss12(t, x, cfg)
    [E1N, E2N, E1W, E2W] = localBandEnergy(t, x, cfg);
    narrow = sqrt(E1N + E2N);
    wide = sqrt(E1W + E2W);
end

function [E1N, E2N, E1W, E2W] = localBandEnergy(t, x, cfg)
    x = x(:) - mean(x(:), 'omitnan');
    N = numel(x);
    X = fft(x);
    f = (0:N-1).' / N * cfg.analysisFs;
    amp2 = @(fc) (2*abs(X(find(abs(f - fc) == min(abs(f - fc), [], 1), 1))) / N)^2;

    E1N = 0;
    E2N = 0;
    for q = -3:3
        E1N = E1N + amp2(cfg.fsw + q*cfg.fundamentalHz);
        E2N = E2N + amp2(2*cfg.fsw + q*cfg.fundamentalHz);
    end
    E1W = localBandSum(t, x, cfg, cfg.fsw - 1000, cfg.fsw + 1000);
    E2W = localBandSum(t, x, cfg, 2*cfg.fsw - 1000, 2*cfg.fsw + 1000);
end

function e = localBandSum(t, x, cfg, f1, f2) %#ok<INUSL>
    x = x(:) - mean(x(:), 'omitnan');
    N = numel(x);
    X = fft(x);
    f = (0:N-1).' / N * cfg.analysisFs;
    idx = find(f > f1 & f < f2);
    e = sum((2*abs(X(idx)) / N).^2);
end

function [jMin, j1Min, j2Min] = localCurveMins(d, cfg)
    jMin = NaN;
    j1Min = NaN;
    j2Min = NaN;
    if ~d.available
        return;
    end
    idx = d.t >= cfg.curveMeanStart;
    y = d.y(idx, :);
    n = size(y, 2);
    if n >= 182
        jcurve = mean(y(:, 2:182), 1, 'omitnan');
        [~, i] = min(jcurve);
        jMin = i - 1;
    end
    if n >= 363
        j1 = mean(y(:, 183:363), 1, 'omitnan');
        [~, i] = min(j1);
        j1Min = i - 1;
    end
    if n >= 544
        j2 = mean(y(:, 364:544), 1, 'omitnan');
        [~, i] = min(j2);
        j2Min = i - 1;
    end
end

function tbl = localBuildTable(results, cfg)
    n = numel(results);
    angleDeg = NaN(n, 1);
    status = strings(n, 1);
    recordFile = strings(n, 1);
    IHMeanA = NaN(n, 1);
    IHRmsA = NaN(n, 1);
    reconJMinDeg = NaN(n, 1);
    reconJ1MinDeg = NaN(n, 1);
    reconJ2MinDeg = NaN(n, 1);
    dcJMinDeg = NaN(n, 1);
    dcJ1MinDeg = NaN(n, 1);
    dcJ2MinDeg = NaN(n, 1);
    fftHighNarrowRss = NaN(n, 1);
    fftHighWideRss = NaN(n, 1);
    fftPsumNarrowRss = NaN(n, 1);
    fftPsumWideRss = NaN(n, 1);
    g1Narrow = NaN(n, 1);
    g2Narrow = NaN(n, 1);
    g1g2Narrow = NaN(n, 1);
    g1Wide = NaN(n, 1);
    g2Wide = NaN(n, 1);
    g1g2Wide = NaN(n, 1);
    validCount = NaN(n, 1);
    observerValidationAvailable = false(n, 1);

    for i = 1:n
        r = results(i);
        angleDeg(i) = r.angleDeg;
        status(i) = r.status;
        recordFile(i) = r.recordFile;
        IHMeanA(i) = r.IHMeanA;
        IHRmsA(i) = r.IHRmsA;
        reconJMinDeg(i) = r.reconJMinDeg;
        reconJ1MinDeg(i) = r.reconJ1MinDeg;
        reconJ2MinDeg(i) = r.reconJ2MinDeg;
        dcJMinDeg(i) = r.dcJMinDeg;
        dcJ1MinDeg(i) = r.dcJ1MinDeg;
        dcJ2MinDeg(i) = r.dcJ2MinDeg;
        fftHighNarrowRss(i) = r.fftHighNarrowRss;
        fftHighWideRss(i) = r.fftHighWideRss;
        fftPsumNarrowRss(i) = r.fftPsumNarrowRss;
        fftPsumWideRss(i) = r.fftPsumWideRss;
        g1Narrow(i) = r.g1Narrow;
        g2Narrow(i) = r.g2Narrow;
        g1g2Narrow(i) = r.g1g2Narrow;
        g1Wide(i) = r.g1Wide;
        g2Wide(i) = r.g2Wide;
        g1g2Wide(i) = r.g1g2Wide;
        validCount(i) = r.validCount;
        observerValidationAvailable(i) = r.observerValidationAvailable;
    end

    tbl = table(angleDeg, status, recordFile, IHMeanA, IHRmsA, ...
        reconJMinDeg, reconJ1MinDeg, reconJ2MinDeg, ...
        dcJMinDeg, dcJ1MinDeg, dcJ2MinDeg, ...
        fftHighNarrowRss, fftHighWideRss, ...
        fftPsumNarrowRss, fftPsumWideRss, ...
        g1Narrow, g2Narrow, g1g2Narrow, ...
        g1Wide, g2Wide, g1g2Wide, validCount, observerValidationAvailable);
    tbl = sortrows(tbl, 'angleDeg');
    tbl.dcHighVoltage = repmat(cfg.dcHighVoltage, height(tbl), 1);
    tbl.dcLowVoltage = repmat(cfg.dcLowVoltage, height(tbl), 1);
    tbl.xi = repmat(cfg.xi, height(tbl), 1);
    tbl.modulationIndex = repmat(cfg.modulationIndex, height(tbl), 1);
end

function r = localResultTemplate()
    r = struct( ...
        'status', "pending", ...
        'errorMessage', "", ...
        'recordFile', "", ...
        'angleDeg', NaN, ...
        'IHMeanA', NaN, 'IHRmsA', NaN, ...
        'reconJMinDeg', NaN, 'reconJ1MinDeg', NaN, 'reconJ2MinDeg', NaN, ...
        'dcJMinDeg', NaN, 'dcJ1MinDeg', NaN, 'dcJ2MinDeg', NaN, ...
        'fftHighNarrowRss', NaN, 'fftHighWideRss', NaN, ...
        'fftPsumNarrowRss', NaN, 'fftPsumWideRss', NaN, ...
        'g1Narrow', NaN, 'g2Narrow', NaN, 'g1g2Narrow', NaN, ...
        'g1Wide', NaN, 'g2Wide', NaN, 'g1g2Wide', NaN, ...
        'validCount', NaN, 'observerValidationAvailable', false);
end
