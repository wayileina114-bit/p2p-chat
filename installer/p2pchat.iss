; P2P 聊天 安装脚本（Inno Setup 6）
; 把 onedir 打包好的 dist\P2PChat 整个文件夹装进 Program Files
; 安装时自动检测已装版本，并询问是否先卸载旧版本

#define MyAppName "P2P聊天"
#define MyAppVersion "1.9.0"
#define MyAppPublisher "P2PChat"
#define MyAppExeName "P2PChat.exe"

[Setup]
AppId={{8F1A4937-D5DB-4826-B59F-4C07241822AA}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 图标（用生成的 P2PChat.ico）
SetupIconFile=..\P2PChat.ico
; 输出路径与文件名
OutputDir=..\installer
OutputBaseFilename=P2PChat-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "cn"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; onedir 整个文件夹打进安装包
Source: "..\dist\P2PChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  AppGUID = '{8F1A4937-D5DB-4826-B59F-4C07241822AA}';

function UninstallRegKey(): string;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + AppGUID + '_is1';
end;

function GetInstalledVersion(): string;
begin
  Result := '';
  if not RegQueryStringValue(HKLM, UninstallRegKey(), 'DisplayVersion', Result) then
    RegQueryStringValue(HKCU, UninstallRegKey(), 'DisplayVersion', Result);
end;

function GetUninstallString(): string;
begin
  Result := '';
  if not RegQueryStringValue(HKLM, UninstallRegKey(), 'UninstallString', Result) then
    RegQueryStringValue(HKCU, UninstallRegKey(), 'UninstallString', Result);
end;

function InitializeSetup(): Boolean;
var
  ver, unins: string;
  code: Integer;
begin
  Result := True;
  ver := GetInstalledVersion();
  if ver <> '' then
  begin
    if MsgBox('检测到本机已安装 {#MyAppName} v' + ver + '。' + #13#10 + #13#10 +
              '是否先卸载旧版本再继续安装？' + #13#10 + #13#10 +
              '（选择「否」将直接覆盖安装；聊天记录与头像保存在程序目录，不受影响）',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      unins := GetUninstallString();
      if unins <> '' then
      begin
        Exec(RemoveQuotes(unins), '/SILENT', '', SW_SHOW, ewWaitUntilTerminated, code);
      end;
    end;
  end;
end;