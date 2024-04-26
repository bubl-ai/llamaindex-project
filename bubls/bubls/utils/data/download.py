import os
from git import Repo


def download_folder_from_repo(
    repo_url: str, repo_name: str, folder_path: str
) -> str:
    """Download a folder from a GitHub repo.

    Args:
        repo_url (str): Full URL of the repo
        repo_name (str): Repository Name
        folder_path (str): PAth of the folder that contains the data to be downloaded

    Raises:
        ValueError: When folder_path is not found in the repo.

    Returns:
        str: The local path where the information is avaialble.
    """
    # Clone the repository
    repo_dst_path = f"/tmp/{repo_name}"
    repo = Repo.clone_from(repo_url, repo_dst_path)

    # Get the folder path within the repository
    repo_folder_path = os.path.join(repo.working_tree_dir, folder_path)

    # Check if the folder exists
    if not os.path.exists(repo_folder_path):
        raise ValueError(
            f"The folder '{folder_path}' does not exist in the repository."
        )

    # Copy the contents of the folder to the destination path
    destination_path = os.path.join(os.environ["DATA_DIR"], repo_name)
    if not os.path.exists(destination_path):
        os.mkdir(destination_path)
    os.system(f"cp -r {repo_folder_path}/* {destination_path}")
    os.system(f"rm -r {repo_dst_path}")

    return destination_path
