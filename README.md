# GUI Tool for Unreal Engine 4 .pak Archives

A modern, easy-to-use GUI for the original `u4pak.py` script. This tool is built using PySide6 to simplify the process of viewing, extracting, testing, and creating `.pak` archives from Unreal Engine 4-based games.

## 📸 Screenshot
<img width="735" height="640" alt="Screenshot 2025-09-20 071256" src="https://github.com/user-attachments/assets/2f6e8e9b-5b61-48f8-9519-3ef5d924fa33" />


## ✨ Features

* **Tab-Based Interface:** Clean design with functionality separated into tabs: Info & Unpack, Pack, and Test.
* **View & Explore:** Open and view a list of all files inside a `.pak` archive in a neat tree-view format.
* **Detailed Information:** Get a summary of archive information, including version, number of files, size, and more.
* **Flexible Extraction:** Extract all files from an archive or select specific files/folders to extract.
* **Creating Archives:** Easily create new `.pak` archives from a collection of files and directories. Options for archive versioning and Zlib compression are available.
* **Integrity Test:** Verifies the checksums of all files within the archive to ensure none are corrupted.
* **Responsive:** Time-consuming operations (such as packing and unpacking) run in a background thread to prevent application crashes.

## 🛠️ System Requirements

* **Python 3.7+**
* **PySide6**

You can install the required libraries using pip:

```bash
pip install PySide6
```

## 🚀 How to Run

1. Make sure Python and PySide6 are installed on your system.
2. Save the script as `u4pak_gui.py`.
3. Open a terminal or command prompt and navigate to the directory where you saved the files.
4. Run the script with the following command:
```bash
python u4pak_gui.py
```

## 📖 User Guide

### Tab: Info & Unpack

1. Select File: Click the "Browse..." button to select the `.pak` file you want to open.
2. Load Contents: Click the "Load & List Contents" button. The archive contents will be displayed in the table below.
3. View Info: Click "Show Archive Info" to view a detailed summary of the archive.
4. Extract: Select the destination directory in the "Unpack to" field.
To extract specific files, select them in the table, then click "Unpack Selected."
To extract all files, click "Unpack All."

### Tab: Pack

1. Add Files/Folders: Use the "Add Files..." or "Add Directory..." buttons to add the items you want to include in the new archive.
2. Set Options:

Select the desired archive format version.
Check "Use Zlib Compression" if you want to compress the files.
Adjust the Mount Point if needed (the default is usually sufficient).
3. Specify Output: Select a location and name for your new `.pak` file in the "Output file" field.
4. Start: Click the "Start Packing" button.

### Tab: Test

1. Select File: Click "Browse..." to select the `.pak` file you want to test for integrity.
2. Run Test: Click the "Run Integrity Test" button.
3. **View Results:** The results of the check will be displayed in the text area below. You will be notified if all files are valid or if any errors were found.

## 📄 License

The original `u4pak.py` script is licensed under the **MIT License**. All modifications and additions to the graphical user interface (GUI) in this file are also subject to the same license.

## 🙏 Credits

* **Core `u4pak.py` Script:** Created by Mathias Panzenböck.
https://github.com/panzi/u4pak

* **GUI Interface (PySide6):** Created and integrated by Danx.
