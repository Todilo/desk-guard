; Desk Guard installer (Inno Setup).
; Per-user install (no admin required) - matches the app's own philosophy of
; not needing elevated rights. Installs to %LocalAppData%\DeskGuard.
;
; Build: "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\DeskGuard.iss
; Output: installer\output\DeskGuardSetup.exe

#define MyAppName "Desk Guard"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Christian Klinton"
#define MyAppURL "https://github.com/Todilo/desk-guard"

[Setup]
AppId={{2C500801-1D36-4A32-9D3E-94E10CFEAA6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\DeskGuard
DefaultGroupName=Desk Guard
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=DeskGuardSetup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\icon.ico
InfoAfterFile=next_steps.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start Desk Guard automatically when I log in"; GroupDescription: "Startup:"; Flags: checkedonce

[Files]
Source: "..\DeskGuard.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\DeskGuardWatchdog.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\desk_guard.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pin_auth.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop_all.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\uninstall.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\face_detection_yunet_2023mar.onnx"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Desk Guard"; Filename: "{app}\DeskGuardWatchdog.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Desk Guard README"; Filename: "{app}\README.md"
Name: "{group}\Uninstall Desk Guard"; Filename: "{uninstallexe}"
Name: "{userstartup}\Desk Guard"; Filename: "{app}\DeskGuardWatchdog.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: autostart

[Run]
Filename: "{app}\DeskGuard.exe"; Parameters: "--enroll"; WorkingDir: "{app}"; Description: "Enroll your face now (opens a webcam preview)"; Flags: postinstall skipifsilent
Filename: "{app}\DeskGuardWatchdog.exe"; WorkingDir: "{app}"; Description: "Start Desk Guard now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Stop watchdog first (harmless - only tray disappearing triggers a lock),
; then tray, so files aren't locked and no lock/relaunch loop is triggered
; by a plain process kill during uninstall.
Filename: "{cmd}"; Parameters: "/C taskkill /IM ""DeskGuardWatchdog.exe"" /F & taskkill /IM ""DeskGuard.exe"" /F"; Flags: runhidden; RunOnceId: "StopDeskGuard"

[UninstallDelete]
; Personal data (faces/, model, settings, PIN, logs) is left in {app} by
; design - uninstalling shouldn't silently delete someone's enrolled face
; data. Run uninstall.py or delete the folder manually for a full wipe.
Type: files; Name: "{app}\tray.pid"
Type: files; Name: "{app}\watchdog.pid"
Type: files; Name: "{app}\shutdown.flag"
Type: files; Name: "{app}\pending_notification.json"
