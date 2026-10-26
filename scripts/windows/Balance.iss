#ifndef AppVersion
  #error Pass /DAppVersion from scripts/build_windows.ps1
#endif

[Setup]
AppId={{943C3887-1673-4A4C-A58C-10C5E5294229}
AppName=Balance
AppVersion={#AppVersion}
AppPublisher=Julius Paulin
AppPublisherURL=https://github.com/JuliusPaulin/balance-app
DefaultDirName={localappdata}\Programs\Balance
DefaultGroupName=Balance
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0.17763
OutputDir=..\..\dist
OutputBaseFilename=Balance-Windows-x64-Setup
SetupIconFile=..\..\static\icon.ico
UninstallDisplayIcon={app}\Balance.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\..\dist\Balance\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\build\windows-prerequisites\VC_redist.x64.exe"; Flags: dontcopy
Source: "..\..\build\windows-prerequisites\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{group}\Balance"; Filename: "{app}\Balance.exe"
Name: "{autodesktop}\Balance"; Filename: "{app}\Balance.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Balance.exe"; Description: "Open Balance"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebViewKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  VCKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';

var
  RuntimeNeedsRestart: Boolean;

function HasWebView2: Boolean;
var
  Version: String;
begin
  Result := (RegQueryStringValue(HKLM32, WebViewKey, 'pv', Version) and
             (Version <> '') and (Version <> '0.0.0.0')) or
            (RegQueryStringValue(HKCU, WebViewKey, 'pv', Version) and
             (Version <> '') and (Version <> '0.0.0.0'));
end;

function HasVCRuntimeAt(RootKey: Integer): Boolean;
var
  Installed, Major, Minor, Build: Cardinal;
begin
  Result := RegQueryDWordValue(RootKey, VCKey, 'Installed', Installed) and (Installed = 1);
  if not Result then Exit;
  Result := RegQueryDWordValue(RootKey, VCKey, 'Major', Major) and
            RegQueryDWordValue(RootKey, VCKey, 'Minor', Minor) and
            RegQueryDWordValue(RootKey, VCKey, 'Bld', Build);
  if Result then
    Result := (Major > {#VCMajor}) or
              ((Major = {#VCMajor}) and (Minor > {#VCMinor})) or
              ((Major = {#VCMajor}) and (Minor = {#VCMinor}) and (Build >= {#VCBuild}));
end;

function HasVCRuntime: Boolean;
begin
  Result := HasVCRuntimeAt(HKLM64) or HasVCRuntimeAt(HKLM32);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code, Attempt: Integer;
begin
  Result := '';
  if not HasVCRuntime then
  begin
    ExtractTemporaryFile('VC_redist.x64.exe');
    if not ShellExec('runas', ExpandConstant('{tmp}\VC_redist.x64.exe'),
        '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    begin
      Result := 'Microsoft Visual C++ Runtime installation was cancelled or could not start.';
      Exit;
    end;
    if (Code <> 0) and (Code <> 3010) then
    begin
      Result := Format('Microsoft Visual C++ Runtime installation failed (code %d).', [Code]);
      Exit;
    end;
    RuntimeNeedsRestart := Code = 3010;
    if not HasVCRuntime then
    begin
      Result := 'Microsoft Visual C++ Runtime is still missing. Restart Windows and run Setup again.';
      Exit;
    end;
  end;
  if not HasWebView2 then
  begin
    ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
        '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    begin
      Result := 'WebView2 installation could not start. Install it from https://developer.microsoft.com/microsoft-edge/webview2/.';
      Exit;
    end;
    { The bootstrapper can exit before installation finishes. Check the runtime. }
    for Attempt := 1 to 120 do
    begin
      if HasWebView2 then Break;
      Sleep(1000);
    end;
    if not HasWebView2 then
      Result := 'WebView2 is still missing. Check your internet connection and run Setup again.';
  end;
end;

function NeedRestart: Boolean;
begin
  Result := RuntimeNeedsRestart;
end;
