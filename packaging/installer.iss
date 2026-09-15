; Yanlin Smart-Creation Matrix · Inno Setup 6 安装器脚本
; 构建顺序：python packaging\build_release.py  →  Inno Setup 编译本文件（或 build_release.py --installer）

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Yanlin Smart-Creation Matrix"
#define ExeName "YanlinMatrix.exe"

[Setup]
AppId={{5E8A2C41-9F3B-4D7E-A6C2-7B1D3E5F8A90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Yanlin
DefaultDirName={autopf}\YanlinMatrix
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
Compression=lzma2
SolidCompression=yes
OutputDir=dist
OutputBaseFilename=YanlinMatrix-Setup-{#AppVersion}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\YanlinMatrix\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
