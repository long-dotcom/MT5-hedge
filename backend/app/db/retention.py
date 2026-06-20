from sqlalchemy.orm import Session


def prune_table_by_id(db: Session, model, keep: int = 1000) -> None:
    if keep <= 0:
        return
    cutoff_id = db.query(model.id).order_by(model.id.desc()).offset(keep).limit(1).scalar()
    if cutoff_id:
        db.query(model).filter(model.id <= cutoff_id).delete(synchronize_session=False)
