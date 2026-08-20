python make_dist.py --entry app_v4.py --out C:\build\dist_v4e --apply
(Get-Item C:\build\dist_v4e\launcher.py).Length                    # 11,986
(Get-Item C:\build\dist_v4e\docshape\packs\petroleum.py).Length    # 62,679
Remove-Item -Recurse -Force C:\build\payload\app
Copy-Item C:\build\dist_v4e C:\build\payload\app -Recurse
Remove-Item C:\build\output\*.exe -ErrorAction SilentlyContinue
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
    /DPayloadDir=C:\build\payload /DOutputDir=C:\build\output .\installer.iss
