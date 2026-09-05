#ifndef AppVersion
  #error AppVersion must be defined by the build script
#endif
#ifndef SourceDir
  #error SourceDir must be defined by the build script
#endif
#ifndef OutputDir
  #error OutputDir must be defined by the build script
#endif

#define AppName "DocSeek"
#define AppExeName "DocSeek.exe"

[Setup]
AppId={{0B62B2CC-67C3-4B9F-BF45-62E7B47F6A79}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=DocSeek
DefaultDirName={localappdata}\Programs\DocSeek
DefaultGroupName=DocSeek
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=DocSeek-{#AppVersion}-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

; The standard runner installation only guarantees Inno Setup's built-in
; English messages. Keep the installer build self-contained; the DocSeek app
; itself remains Chinese-first. A vetted Chinese .isl can be added later as a
; versioned source file instead of being downloaded opportunistically at build time.
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\DocSeek"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\DocSeek"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 DocSeek"; Flags: nowait postinstall skipifsilent
