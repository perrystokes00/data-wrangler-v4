; installer.iss — Data Wrangler v4, Windows installer
;
; Compile with Inno Setup 6:  ISCC.exe installer.iss
; build_installer.ps1 does this for you after assembling the payload.
;
; WHAT THIS PRODUCES
;   A normal Windows installer: Program Files by default, Start Menu entry,
;   optional desktop icon, and an entry in Apps & Features that removes
;   cleanly. No Python required on the machine — the payload carries its
;   own.
;
; INSTALLS ALONGSIDE v2, DELIBERATELY
;   The AppId below is NOT v2's. Inno treats a matching AppId as an upgrade
;   and runs the old uninstaller first, which would remove a working v2 to
;   install a different generation of the product. Everything a customer
;   could collide on is versioned: the install folder, the Start Menu group,
;   the uninstall entry, and the per-user data folder. The two never meet.
;
; THE PREREQUISITE THAT CANNOT BE BUNDLED
;   The Microsoft ODBC Driver for SQL Server is not redistributable inside
;   a third-party installer. Without it the app installs perfectly and can
;   connect to nothing, which is the worst kind of failure: everything
;   looks fine. So this checks for it and says so plainly BEFORE installing
;   rather than leaving the customer to discover it at the connect screen.

#define AppName        "Data Wrangler v4"
#define AppVersion     "4.0.0"
#define AppPublisher   "Data Wrangler Solutions LLC"
#define AppURL         "https://datawranglersolutions.com"

; MUST MATCH DATA_FOLDER in launcher.py. If these disagree the installer
; creates a folder nothing uses, the app makes its own on first run, and the
; uninstaller then preserves the wrong one. Nothing reports the mismatch.
#define DataFolder     "DataWranglerV4"

; Paths are OVERRIDABLE from the command line and default to ABSOLUTE:
;   ISCC.exe /DPayloadDir=C:\build\payload /DOutputDir=C:\build\output installer.iss
; A relative default here has the same defect the .ps1 had — it silently
; means something different depending on where the compiler was invoked, and
; a payload that resolves to nothing packages an empty tree without error.
#ifndef PayloadDir
  #define PayloadDir   "C:\build\payload"
#endif
#ifndef OutputDir
  #define OutputDir    "C:\build\output"
#endif

; Shortcut icons must be .ico, an .exe or a .dll — Windows silently ignores
; a .png. v2 shipped a real data_wrangler.ico; if it has been copied into
; the app tree, use it, and fall back to the interpreter's own icon so the
; shortcut is never blank. Decided AT COMPILE TIME, so a missing icon can
; never produce a broken shortcut on the customer's machine.
#define IcoRel         "app\assets\data_wrangler.ico"
#if FileExists(AddBackslash(PayloadDir) + IcoRel)
  #define ShortcutIcon "{app}\" + IcoRel
  #define SetupIco     AddBackslash(PayloadDir) + IcoRel
#else
  #pragma message "No data_wrangler.ico in the payload — falling back to pythonw.exe"
  #define ShortcutIcon "{app}\python\pythonw.exe"
#endif

[Setup]
; Generated for v4. NOT v2's AppId — see the header.
AppId={{8F3A1C42-7E5B-4D96-A1F2-2C6B9D4E7A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
UninstallDisplayName={#AppName}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename=DataWranglerV4-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit only: the embeddable python is amd64, and so are the pyodbc,
; pandas and shapely wheels that ship with it.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-machine needs admin; that is the right default for Program Files.
; For a per-user install with no elevation, set PrivilegesRequired=lowest
; and DefaultDirName={autopf} resolves to the user's own folder.
PrivilegesRequired=admin
DisableProgramGroupPage=yes
; Points at a file that EXISTS. The old value named DataWrangler.exe, which
; nothing in the payload ever produced, so Apps & Features showed a blank
; icon — a compile-clean error that only shows up after installing.
UninstallDisplayIcon={#ShortcutIcon}
#ifdef SetupIco
; The icon on setup.exe itself, which is the first thing a customer sees.
SetupIconFile={#SetupIco}
#endif
; Windows 10 or later — the same floor v2 set, and what the embeddable
; python and the amd64 wheels expect.
MinVersion=10.0
DiskSpanning=no
; SIGN THIS. Without a certificate SmartScreen shows "Windows protected
; your PC" and most customers stop there.
; SignTool=signtool sign /f cert.pfx /p $p /tr http://timestamp.digicert.com /td sha256 /fd sha256 $f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The whole payload: private python + the app tree.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; pythonw.exe runs the launcher with NO console window. The launcher then
; starts the real server with python.exe hidden behind it — streamlit needs
; a working stdout, which pythonw does not provide.
Name: "{group}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; \
    Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; \
    IconFilename: "{#ShortcutIcon}"
Name: "{group}\{#AppName} log folder"; Filename: "{localappdata}\{#DataFolder}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; \
    Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; \
    IconFilename: "{#ShortcutIcon}"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; \
    Description: "Start {#AppName} now"; WorkingDir: "{app}\app"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Compiled bytecode is generated after install, so the uninstaller does not
; know about it and would leave the folders behind.
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\__pycache__"
Type: filesandordirs; Name: "{app}\app\__pycache__"
; NOTE: {localappdata}\{#DataFolder} is deliberately NOT removed. It holds
; the user's settings and logs; an uninstall that also destroys those makes
; a reinstall-to-fix-something lose the configuration too.

[Code]
function OdbcDriverInstalled(): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
begin
  Result := False;
  // Every installed ODBC driver appears as a value under this key. Check
  // for 17 or 18 by name rather than a version number, because Microsoft
  // ships them side by side and either will do.
  if RegGetValueNames(HKLM, 'SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers', Names) then
  begin
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      if (Pos('ODBC Driver 17 for SQL Server', Names[I]) > 0) or
         (Pos('ODBC Driver 18 for SQL Server', Names[I]) > 0) then
      begin
        Result := True;
        Exit;
      end;
    end;
  end;
end;

function InitializeSetup(): Boolean;
var
  Answer: Integer;
begin
  Result := True;
  if not OdbcDriverInstalled() then
  begin
    Answer := MsgBox(
      'The Microsoft ODBC Driver for SQL Server (17 or 18) was not found.' + #13#10 + #13#10 +
      '{#AppName} needs it to connect to a database. It cannot be included ' +
      'in this installer — Microsoft distributes it separately.' + #13#10 + #13#10 +
      'You can continue and install the driver afterwards, but the ' +
      'application will not connect until you do.' + #13#10 + #13#10 +
      'Continue anyway?',
      mbConfirmation, MB_YESNO);
    if Answer = IDNO then
    begin
      // Open the download page on the way out, so "no" is still useful.
      ShellExec('open',
        'https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server',
        '', '', SW_SHOW, ewNoWait, Answer);
      Result := False;
    end;
  end;
end;

// NOTE — there is deliberately no ssPostInstall step creating
// {localappdata}\{#DataFolder}. launcher.py's data_dir() does
// mkdir(parents=True, exist_ok=True) on every start, so the folder always
// exists by the time anything writes to it. Creating it here as well was
// not just redundant: PrivilegesRequired=admin means {localappdata}
// resolves to whichever account is ELEVATED, so an admin installing for a
// different user would have created it in the wrong profile — and ISCC
// warns about exactly that ("UsedUserAreasWarning"). One owner for the
// folder, and it is the code that writes to it.
