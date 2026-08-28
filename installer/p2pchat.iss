; P2P 聊天 安装脚本（Inno Setup 6）
; 把 onedir 打包好的 dist\P2PChat 整个文件夹装进 Program Files

#define MyAppName "P2P聊天"
#define MyAppVersion "1.4.0"
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