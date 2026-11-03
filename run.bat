@echo off
setlocal enabledelayedexpansion

REM Set folder names
set "NEW_FOLDER=Serriff"
set "OLD_FOLDER=Serriff Older"
set "REPO_URL=https://github.com/rohitborasuntec/Serriff"

echo ========================================
echo Starting deployment process...
echo ========================================

REM Step 1: Check if Serriff folder exists
if exist "%NEW_FOLDER%" (
    echo Found existing "%NEW_FOLDER%" folder. Renaming to "%OLD_FOLDER%"...
    
    REM Check if old folder already exists and remove it
    if exist "%OLD_FOLDER%" (
        echo Warning: "%OLD_FOLDER%" already exists. Removing it first...
        rmdir /s /q "%OLD_FOLDER%"
    )
    
    rename "%NEW_FOLDER%" "%OLD_FOLDER%"
    if errorlevel 1 (
        echo ERROR: Failed to rename folder. Make sure no files are open in "%NEW_FOLDER%".
        pause
        exit /b 1
    )
    echo Renamed successfully.
) else (
    echo No existing "%NEW_FOLDER%" folder found. Skipping rename.
)

REM Step 2: Clone the repository
echo.
echo Cloning repository...
git clone %REPO_URL%
if errorlevel 1 (
    echo ERROR: Git clone failed. Please check your internet connection and git installation.
    pause
    exit /b 1
)
echo Clone completed successfully.

REM Step 3: Move files from old folder to new folder
if exist "%OLD_FOLDER%" (
    echo.
    echo Moving files from "%OLD_FOLDER%" to "%NEW_FOLDER%"...
    
    REM Move "Output Data" folder
    if exist "%OLD_FOLDER%\Output Data" (
        echo Moving "Output Data"...
        move "%OLD_FOLDER%\Output Data" "%NEW_FOLDER%\" >nul
        if errorlevel 1 echo WARNING: Could not move "Output Data"
    ) else (
        echo "Output Data" not found. Skipping.
    )
    
    REM Move "Screenshots" folder
    if exist "%OLD_FOLDER%\Screenshots" (
        echo Moving "Screenshots"...
        move "%OLD_FOLDER%\Screenshots" "%NEW_FOLDER%\" >nul
        if errorlevel 1 echo WARNING: Could not move "Screenshots"
    ) else (
        echo "Screenshots" not found. Skipping.
    )
    
    REM Move "Logs" folder
    if exist "%OLD_FOLDER%\Logs" (
        echo Moving "Logs"...
        move "%OLD_FOLDER%\Logs" "%NEW_FOLDER%\" >nul
        if errorlevel 1 echo WARNING: Could not move "Logs"
    ) else (
        echo "Logs" not found. Skipping.
    )
    
    REM Move ".env" file
    if exist "%OLD_FOLDER%\.env" (
        echo Moving ".env"...
        move "%OLD_FOLDER%\.env" "%NEW_FOLDER%\" >nul
        if errorlevel 1 echo WARNING: Could not move ".env"
    ) else (
        echo ".env" not found. Skipping.
    )
    
    REM Move "date.json" file
    if exist "%OLD_FOLDER%\date.json" (
        echo Moving "date.json"...
        move "%OLD_FOLDER%\date.json" "%NEW_FOLDER%\" >nul
        if errorlevel 1 echo WARNING: Could not move "date.json"
    ) else (
        echo "date.json" not found. Skipping.
    )
    
    REM Step 4: Delete the old folder
    echo.
    echo Deleting "%OLD_FOLDER%" folder...
    rmdir /s /q "%OLD_FOLDER%"
    if errorlevel 1 (
        echo WARNING: Could not fully delete "%OLD_FOLDER%". Some files may be in use.
    ) else (
        echo Old folder deleted successfully.
    )
) else (
    echo.
    echo No old folder to migrate from. Skipping file migration.
)

echo.
echo ========================================
echo Process completed!
echo ========================================
endlocal
exit /b 1