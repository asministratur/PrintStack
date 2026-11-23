#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "Printstack"
#define AppPublisher "asministratur"
#define AppURL "https://github.com/asministratur/PrintStack"
#define AppExeName "Printstack.exe"

[Setup]
AppId={{4C1A9F82-7E31-4B28-92A1-6E04D8C5B219}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={autopf}\Printstack
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Printstack-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
WizardImageFile=..\assets\installer-banner.bmp
WizardSmallImageFile=..\assets\installer-icon.bmp
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "sendtoicon"; Description: "Add to Windows 'Send to' menu"; GroupDescription: "Windows Explorer integration:"
Name: "contextmenu"; Description: "Add 'Print with Printstack' to Explorer right-click menu"; GroupDescription: "Windows Explorer integration:"

[Files]
Source: "..\dist\Printstack\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{usersendto}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: sendtoicon

[Registry]
; Directory context menu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Printstack"; ValueType: string; ValueData: "Print with Printstack"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Printstack"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName}"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Printstack\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu

; Supported file extensions
#define AddExtCtx(str Ext) \
  "Root: HKCU; Subkey: ""Software\Classes\SystemFileAssociations\" + Ext + "\shell\Printstack""; ValueType: string; ValueData: ""Print with Printstack""; Flags: uninsdeletekey; Tasks: contextmenu" + NewLine + \
  "Root: HKCU; Subkey: ""Software\Classes\SystemFileAssociations\" + Ext + "\shell\Printstack""; ValueType: string; ValueName: ""Icon""; ValueData: ""{app}\" + AppExeName + """; Tasks: contextmenu" + NewLine + \
  "Root: HKCU; Subkey: ""Software\Classes\SystemFileAssociations\" + Ext + "\shell\Printstack\command""; ValueType: string; ValueData: """"""{app}\" + AppExeName + """"" """"%1""""""; Tasks: contextmenu" + NewLine

#emit AddExtCtx(".pdf")
#emit AddExtCtx(".docx")
#emit AddExtCtx(".doc")
#emit AddExtCtx(".rtf")
#emit AddExtCtx(".jpg")
#emit AddExtCtx(".jpeg")
#emit AddExtCtx(".png")
#emit AddExtCtx(".bmp")
#emit AddExtCtx(".webp")
#emit AddExtCtx(".tif")
#emit AddExtCtx(".tiff")

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
