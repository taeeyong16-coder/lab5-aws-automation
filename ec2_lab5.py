#!/usr/bin/env python3
import argparse
import os
import stat
import time
from typing import Optional, List, Dict

import boto3
from botocore.exceptions import ClientError


def get_latest_amzn2_arm64_ami(region: str) -> str:
    """
    Беремо актуальну Amazon Linux 2 ARM64 AMI через SSM Parameter Store.
    Працює стабільніше, ніж хардкодити ImageId.
    """
    ssm = boto3.client("ssm", region_name=region)
    param_name = "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-arm64-gp2"
    resp = ssm.get_parameter(Name=param_name)
    return resp["Parameter"]["Value"]


def create_key_pair(region: str, key_name: str, out_path: str) -> str:
    """
    Створює EC2 Key Pair і зберігає приватний ключ у файл out_path.
    Встановлює права 400.
    """
    ec2 = boto3.client("ec2", region_name=region)

    try:
        resp = ec2.create_key_pair(KeyName=key_name, KeyType="rsa")
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidKeyPair.Duplicate":
            raise SystemExit(
                f"KeyPair '{key_name}' вже існує в AWS. "
                f"Візьми іншу назву або видали старий key pair у консолі."
            )
        raise

    private_key = resp["KeyMaterial"]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(private_key)

    # chmod 400
    os.chmod(out_path, stat.S_IRUSR)

    return out_path


def ensure_security_group(region: str, sg_name: str, ssh_cidr: str, vpc_id: Optional[str] = None) -> str:
    """
    Створює security group (або знаходить існуючу) і відкриває SSH(22) тільки з ssh_cidr.
    """
    ec2 = boto3.client("ec2", region_name=region)

    # якщо vpc_id не дали — беремо default VPC
    if not vpc_id:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise SystemExit("Не знайдено default VPC. Вкажи --vpc-id явно.")
        vpc_id = vpcs[0]["VpcId"]

    # перевіряємо, чи вже є SG з такою назвою у VPC
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [sg_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]

    if existing:
        sg_id = existing[0]["GroupId"]
    else:
        resp = ec2.create_security_group(
            GroupName=sg_name,
            Description="Lab5 SG: allow SSH only from my IP",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]

    # додаємо inbound правило на 22 порт (якщо вже є — ігноруємо помилку)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH from my IP"}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("InvalidPermission.Duplicate", "InvalidPermission.Malformed"):
            # Duplicate — вже є правило
            # Malformed — якщо CIDR кривий
            if e.response["Error"]["Code"] == "InvalidPermission.Malformed":
                raise SystemExit(f"Невірний ssh_cidr='{ssh_cidr}'. Приклад: 46.231.224.251/32")
        else:
            raise

    return sg_id


def create_instance(
    region: str,
    name: str,
    key_name: str,
    sg_id: str,
    instance_type: str = "t4g.nano",
) -> str:
    """
    Запускає EC2 інстанс Amazon Linux 2 ARM64 (для t4g.*) і тегує його Name.
    """
    ec2 = boto3.client("ec2", region_name=region)

    ami_id = get_latest_amzn2_arm64_ami(region)

    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        KeyName=key_name,
        SecurityGroupIds=[sg_id],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": name}],
            }
        ],
    )

    instance_id = resp["Instances"][0]["InstanceId"]

    # Чекаємо, поки стане running
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    return instance_id


def get_public_ip(region: str, instance_id: str) -> Optional[str]:
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    return inst.get("PublicIpAddress")


def list_running_t4g(region: str) -> List[Dict[str, str]]:
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
            {"Name": "instance-type", "Values": ["t4g.nano"]},
        ]
    )

    rows = []
    for r in resp["Reservations"]:
        for i in r["Instances"]:
            rows.append(
                {
                    "InstanceId": i["InstanceId"],
                    "Type": i["InstanceType"],
                    "PublicIp": i.get("PublicIpAddress", "-"),
                    "PrivateIp": i.get("PrivateIpAddress", "-"),
                }
            )
    return rows


def stop_instance(region: str, instance_id: str) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    ec2.stop_instances(InstanceIds=[instance_id])
    waiter = ec2.get_waiter("instance_stopped")
    waiter.wait(InstanceIds=[instance_id])


def terminate_instance(region: str, instance_id: str) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    ec2.terminate_instances(InstanceIds=[instance_id])
    waiter = ec2.get_waiter("instance_terminated")
    waiter.wait(InstanceIds=[instance_id])


def main():
    p = argparse.ArgumentParser(description="Lab5 EC2 automation (boto3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Create key pair + SG + EC2 instance")
    c.add_argument("--region", required=True)
    c.add_argument("--name", required=True, help="Instance Name tag")
    c.add_argument("--key-name", required=True)
    c.add_argument("--key-out", required=True, help="Where to save .pem")
    c.add_argument("--sg-name", required=True)
    c.add_argument("--ssh-cidr", required=True, help="Your public IP in CIDR, e.g. 46.231.224.251/32")
    c.add_argument("--vpc-id", default=None)

    l = sub.add_parser("list", help="List running t4g.nano instances")
    l.add_argument("--region", required=True)

    s = sub.add_parser("stop", help="Stop an instance")
    s.add_argument("--region", required=True)
    s.add_argument("--instance-id", required=True)

    t = sub.add_parser("terminate", help="Terminate an instance")
    t.add_argument("--region", required=True)
    t.add_argument("--instance-id", required=True)

    args = p.parse_args()

    if args.cmd == "create":
        pem_path = create_key_pair(args.region, args.key_name, args.key_out)
        sg_id = ensure_security_group(args.region, args.sg_name, args.ssh_cidr, args.vpc_id)
        instance_id = create_instance(args.region, args.name, args.key_name, sg_id)

        public_ip = get_public_ip(args.region, instance_id)

        print("\n=== CREATED ===")
        print(f"KeyPair:     {args.key_name}")
        print(f"PEM saved:   {pem_path} (chmod 400 applied)")
        print(f"SecurityGrp: {args.sg_name} ({sg_id}), SSH allowed from {args.ssh_cidr}")
        print(f"InstanceId:  {instance_id}")
        print(f"Public IP:   {public_ip}")

        if public_ip:
            print("\nSSH command:")
            print(f"ssh -i {pem_path} ec2-user@{public_ip}")

    elif args.cmd == "list":
        rows = list_running_t4g(args.region)
        if not rows:
            print("No running t4g.nano instances found.")
        else:
            for r in rows:
                print(f"{r['InstanceId']}  {r['Type']}  public={r['PublicIp']}  private={r['PrivateIp']}")

    elif args.cmd == "stop":
        stop_instance(args.region, args.instance_id)
        print(f"Stopped: {args.instance_id}")

    elif args.cmd == "terminate":
        terminate_instance(args.region, args.instance_id)
        print(f"Terminated: {args.instance_id}")


if __name__ == "__main__":
    main()
