; Inno Setup 설치 파일 스크립트 (CI 에서 iscc /DAppVersion=x.y.z packaging\installer.iss)
#ifndef AppVersion
  #define AppVersion "0.1.1"
#endif

[Setup]
AppId={{6E0C7E0B-5B1F-4C1E-9D8E-3B8C4C1F2A10}
AppName=LoL Recorder
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\LoLRecorder
DefaultGroupName=LoL Recorder
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LoLRecorder-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\LoLRecorder.exe

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 작업:"

[Files]
Source: "..\dist\LoLRecorder\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\LoL Recorder"; Filename: "{app}\LoLRecorder.exe"
Name: "{group}\LoL Recorder 제거"; Filename: "{uninstallexe}"
Name: "{userdesktop}\LoL Recorder"; Filename: "{app}\LoLRecorder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LoLRecorder.exe"; Description: "LoL Recorder 실행"; Flags: nowait postinstall skipifsilent
