# Workbench for Laravel

MySQL Workbench plugin that exports the current model as Laravel migrations
(Laravel 11+, MySQL only). Each export generates only the changes made since
the previous one.


## Requirements

- MySQL Workbench 8
- A Laravel 11+ project using a MySQL connection


## Installation

1. Download `export_laravel_migrations.py`.
2. Open MySQL Workbench and go to **Scripting > Install Plugin/Module...**
3. Select the downloaded file and confirm.
4. Restart MySQL Workbench.

To update the plugin, repeat the steps above with the new file. To remove it,
use **Scripting > Plugin Manager**.


## Usage

1. Open your model (`.mwb`) in Workbench.
2. Go to **Tools > Catalog > Export Laravel Migration**.
3. Choose one of the actions:
   - **Generate Migration to Folder...**: select your project's
     `database/migrations` folder. A new migration file is created there.
   - **Save Current Model as Baseline...**: use this once for a project whose
     database already exists. It records the current model without creating a
     migration, so later exports contain only new changes.
4. Review the generated file, then run `php artisan migrate`.


## Notes

- The plugin stores its export history in `laravel_workbench_history.json`,
  inside the migrations folder. Commit this file together with the migrations.
- Existing migration files are never overwritten.
- Export one schema at a time.
- Renames are generated as drop + add, and `down()` restores structure only,
  not deleted data.
