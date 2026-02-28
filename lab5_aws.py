import argparse
import os
import sys
import time
import boto3
from botocore.exceptions import ClientError

REGION = "eu-north-1"
AMI_ID = "ami-073130f74f5ffb161"
INSTANCE_TYPE = "t3.micro"
KEY_NAME = "lab5-keypair"
KEY_PATH = os.path.expanduser(f"~/lab5/{KEY_NAME}.pem")
BUCKET_NAME = "lab5-pryshchepa-2026-ua-123456"
DEFAULT_OBJECT_KEY = "rates_2022.csv"

def die(msg):
    print(msg)
    sys.exit(1)

def ec2():
    return boto3.client("ec2", region_name=REGION)

def s3():
    return boto3.client("s3", region_name=REGION)

def ensure_key_pair():
    client = ec2()
    try:
        client.describe_key_pairs(KeyNames=[KEY_NAME])
        return
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "InvalidKeyPair.NotFound":
            die(str(e))
    resp = client.create_key_pair(KeyName=KEY_NAME)
    private_key = resp["KeyMaterial"]
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    with os.fdopen(fd, "w") as f:
        f.write(private_key)

def create_instance():
    ensure_key_pair()
    client = ec2()
    try:
        resp = client.run_instances(
            ImageId=AMI_ID,
            MinCount=1,
            MaxCount=1,
            InstanceType=INSTANCE_TYPE,
            KeyName=KEY_NAME,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "lab5-demo"}]}
            ],
        )
        return resp["Instances"][0]["InstanceId"]
    except ClientError as e:
        print(e)
        return None

def wait_instance_state(instance_id, state):
    client = ec2()
    while True:
        resp = client.describe_instances(InstanceIds=[instance_id])
        current = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        if current == state:
            return
        time.sleep(5)

def get_public_ip(instance_id):
    client = ec2()
    resp = client.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    print(inst.get("PublicIpAddress"))

def stop_instance(instance_id):
    ec2().stop_instances(InstanceIds=[instance_id])

def terminate_instance(instance_id):
    ec2().terminate_instances(InstanceIds=[instance_id])

def create_bucket(name):
    try:
        s3().create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
    except ClientError as e:
        print(e)

def upload_file(bucket, file_path, key):
    try:
        s3().upload_file(Filename=file_path, Bucket=bucket, Key=key)
    except ClientError as e:
        print(e)

def read_csv_head(bucket, key):
    import pandas as pd
    try:
        obj = s3().get_object(Bucket=bucket, Key=key)
        df = pd.read_csv(obj["Body"])
        print(df.head())
    except ClientError as e:
        print(e)

def empty_bucket(bucket):
    client = s3()
    resp = client.list_objects_v2(Bucket=bucket)
    if "Contents" in resp:
        for obj in resp["Contents"]:
            client.delete_object(Bucket=bucket, Key=obj["Key"])

def delete_bucket(bucket):
    empty_bucket(bucket)
    try:
        s3().delete_bucket(Bucket=bucket)
    except ClientError as e:
        print(e)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("ec2-create")
    sub.add_parser("ec2-list")

    ip_p = sub.add_parser("ec2-ip")
    ip_p.add_argument("--instance-id")

    stop_p = sub.add_parser("ec2-stop")
    stop_p.add_argument("--instance-id")

    term_p = sub.add_parser("ec2-terminate")
    term_p.add_argument("--instance-id")

    s3c = sub.add_parser("s3-create")
    s3c.add_argument("--bucket", default=BUCKET_NAME)

    up = sub.add_parser("s3-upload")
    up.add_argument("--bucket", default=BUCKET_NAME)
    up.add_argument("--file")
    up.add_argument("--key", default=DEFAULT_OBJECT_KEY)

    read = sub.add_parser("s3-read")
    read.add_argument("--bucket", default=BUCKET_NAME)
    read.add_argument("--key", default=DEFAULT_OBJECT_KEY)

    delb = sub.add_parser("s3-delete")
    delb.add_argument("--bucket", default=BUCKET_NAME)

    args = parser.parse_args()

    if args.cmd == "ec2-create":
        iid = create_instance()
        if iid:
            wait_instance_state(iid, "running")
            print(iid)
            get_public_ip(iid)

    elif args.cmd == "ec2-stop":
        stop_instance(args.instance_id)
        wait_instance_state(args.instance_id, "stopped")

    elif args.cmd == "ec2-terminate":
        terminate_instance(args.instance_id)
        wait_instance_state(args.instance_id, "terminated")

    elif args.cmd == "s3-create":
        create_bucket(args.bucket)

    elif args.cmd == "s3-upload":
        upload_file(args.bucket, args.file, args.key)

    elif args.cmd == "s3-read":
        read_csv_head(args.bucket, args.key)

    elif args.cmd == "s3-delete":
        delete_bucket(args.bucket)
