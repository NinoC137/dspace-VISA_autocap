%% dspace_selftest.m - 首次联调：验证 MATLAB -> ControlDesk -> MicroLabBox 链路
% 只做四件事：连接、上线、读参数、录 1 s 并读回。除 goOnline 外不写任何参数，
% 不下载实时应用（需要时把 cfg.reloadApplication 设为 true）。
%
% 运行前提：
%   * MicroLabBox 已上电并已在 ControlDesk Platform Manager 中注册；
%   * pacc_drive.rta 已在 MicroLabBox 上运行（Simulink Ctrl+B 或 ControlDesk 已 Load）；
%   * 台架处于安全状态（本脚本不会改 enable 等参数，但会让 ControlDesk 上线）。

clearvars; clc;

cfg.project = 'Project_PACC';
cfg.experiment = 'Experiment_PACC';
cfg.sdfFile = 'F:\ZYS_HYBASE\pacc_drive.sdf';
cfg.reloadApplication = false;
cfg.probeParam = 'Model Root/phi2_deg/Value';         % TRC 中确认存在 (PARAM)
cfg.probeSignals = {'Model Root/PHV/In1', 'Model Root/PLV/In1', 'Model Root/idc1/In1'};
cfg.outDir = fullfile('F:\ZYS_HYBASE\automation\selftest');

rig = DspaceRig();
rig.openExperiment(cfg.project, cfg.experiment);
fprintf('platform = "%s", raster = "%s"\n', rig.platformName, rig.raster);

if cfg.reloadApplication
    rig.loadApplication(cfg.sdfFile, true);
end
fprintf('application state = %g (1 = Running)\n', rig.applicationState());

rig.goOnline();
cleanupObj = onCleanup(@() rig.goOffline()); %#ok<NASGU>

% 1) 读一个参数（不写）
v = rig.getParam(cfg.probeParam);
fprintf('%s = %.6g\n', cfg.probeParam, v);

% 2) 列出全部可写常量，作为 仿真块名 -> 硬件路径 映射的依据
tbl = rig.listVariables('/Value$');
disp(tbl(1:min(40, height(tbl)), :));
writetable(tbl, fullfile(cfg.outDir, 'writable_values.csv'));

% 3) 录 1 s 并读回
rig.configureMeasurement(cfg.probeSignals);
matFile = rig.record(1.0, fullfile(cfg.outDir, 'selftest_rec'));
rec = DspaceRig.loadRecording(matFile);
fprintf('录制含 %d 个信号, 时间轴 %d 点, dt = %.3g s\n', numel(rec.signals), ...
    numel(rec.time{1}), median(diff(rec.time{1})));
for k = 1:numel(rec.signals)
    fprintf('  [%d] %s  (raster %s, %d 点)\n', k, rec.signals(k).key, ...
        rec.signals(k).raster, numel(rec.signals(k).data));
end
d = DspaceRig.getSignal(rec, cfg.probeSignals{1});
if d.available
    figure; plot(d.t, d.y); grid on; xlabel('t / s'); title(cfg.probeSignals{1}, 'Interpreter', 'none');
end
fprintf('selftest OK\n');
